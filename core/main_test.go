package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestVODPrefetchAndRangeHit(t *testing.T) {
	payload := bytes.Repeat([]byte("0123456789ABCDEF"), 4096) // 64 KB
	var upstreamCalls atomic.Int32

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalls.Add(1)
		http.ServeContent(w, r, "movie.mp4", time.Now(), bytes.NewReader(payload))
	}))
	defer upstream.Close()

	proxy := NewStreamProxy()
	engine := NewBackgroundEngine(proxy.client)
	core := httptest.NewServer(newServer(proxy, engine))
	defer core.Close()

	// Trigger prefetch
	body, _ := json.Marshal(map[string]any{"url": upstream.URL + "/movie.mp4"})
	resp, err := http.Post(core.URL+"/prefetch", "application/json", bytes.NewReader(body))
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("prefetch failed: %v", err)
	}
	var pfResp struct {
		ProxyURL string `json:"proxy_url"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&pfResp)
	resp.Body.Close()

	// Wait briefly for background prefetch goroutine
	time.Sleep(100 * time.Millisecond)

	// Request a byte range via proxy — should be served straight from memory cache (HIT-RANGE)
	req, _ := http.NewRequest(http.MethodGet, pfResp.ProxyURL, nil)
	req.Header.Set("Range", "bytes=0-1023")
	r2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("range request failed: %v", err)
	}
	defer r2.Body.Close()
	got, _ := io.ReadAll(r2.Body)
	if r2.StatusCode != http.StatusPartialContent {
		t.Fatalf("expected 206, got %d", r2.StatusCode)
	}
	if r2.Header.Get("X-Stream-Core") != "HIT-RANGE" {
		t.Fatalf("expected HIT-RANGE, got %q", r2.Header.Get("X-Stream-Core"))
	}
	if !bytes.Equal(got, payload[:1024]) {
		t.Fatalf("range payload mismatch")
	}
	if upstreamCalls.Load() != 1 {
		t.Fatalf("expected exactly 1 upstream fetch (prefetch), got %d", upstreamCalls.Load())
	}
}

func TestHLSRewriteAndSegmentPrefetch(t *testing.T) {
	segData := []byte("FAKE_TS_SEGMENT_BYTES")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, ".m3u8") {
			w.Header().Set("Content-Type", "application/vnd.apple.mpegurl")
			fmt.Fprint(w, "#EXTM3U\n#EXTINF:4.0,\nseg0.ts\n")
			return
		}
		w.Header().Set("Content-Type", "video/mp2t")
		_, _ = w.Write(segData)
	}))
	defer upstream.Close()

	proxy := NewStreamProxy()
	engine := NewBackgroundEngine(proxy.client)
	core := httptest.NewServer(newServer(proxy, engine))
	defer core.Close()

	playlistURL := core.URL + "/proxy?url=" + url.QueryEscape(upstream.URL+"/live/index.m3u8")
	resp, err := http.Get(playlistURL)
	if err != nil {
		t.Fatalf("hls fetch failed: %v", err)
	}
	rewritten, _ := io.ReadAll(resp.Body)
	resp.Body.Close()

	if !strings.Contains(string(rewritten), "/proxy?url=") {
		t.Fatalf("expected rewritten proxy URL in playlist, got:\n%s", string(rewritten))
	}

	time.Sleep(100 * time.Millisecond)
	segProxyURL := core.URL + "/proxy?url=" + url.QueryEscape(upstream.URL+"/live/seg0.ts")
	sresp, err := http.Get(segProxyURL)
	if err != nil {
		t.Fatalf("segment fetch failed: %v", err)
	}
	defer sresp.Body.Close()
	gotSeg, _ := io.ReadAll(sresp.Body)
	if sresp.Header.Get("X-Stream-Core") != "HIT-FULL" {
		t.Fatalf("expected prefetched segment HIT-FULL, got %q", sresp.Header.Get("X-Stream-Core"))
	}
	if !bytes.Equal(gotSeg, segData) {
		t.Fatalf("segment mismatch")
	}
}

func TestResolveCacheAndFastSearch(t *testing.T) {
	proxy := NewStreamProxy()
	engine := NewBackgroundEngine(proxy.client)
	core := httptest.NewServer(newServer(proxy, engine))
	defer core.Close()

	// Sync index
	idxBody, _ := json.Marshal(map[string]any{
		"titles": []TitleDoc{
			{Name: "RRR (2022)", Group: "Telugu", URL: "http://x/1"},
			{Name: "RRR Hindi (2022)", Group: "Hindi", URL: "http://x/2"},
			{Name: "Kalki 2898 AD (2024)", Group: "Telugu", URL: "http://x/3"},
		},
	})
	r, err := http.Post(core.URL+"/index/sync", "application/json", bytes.NewReader(idxBody))
	if err != nil || r.StatusCode != http.StatusOK {
		t.Fatalf("index sync failed: %v", err)
	}
	r.Body.Close()

	// Search with Telugu priority
	sr, err := http.Get(core.URL + "/search?q=rrr&langs=telugu,hindi")
	if err != nil {
		t.Fatalf("search failed: %v", err)
	}
	var sres struct {
		Results []TitleDoc `json:"results"`
	}
	_ = json.NewDecoder(sr.Body).Decode(&sres)
	sr.Body.Close()
	if len(sres.Results) != 2 || sres.Results[0].Name != "RRR (2022)" {
		t.Fatalf("unexpected search order: %+v", sres.Results)
	}
}
