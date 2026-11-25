package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	prefetchBytes   = 2 * 1024 * 1024 // 2 MB initial VOD head prefetch for instant FFmpeg probe
	maxCacheBytes   = 48 * 1024 * 1024 // 48 MB total memory cap (safe for 1.6 GB Fire TV Stick)
	hlsPrefetchSegs = 3                // Prefetch first 3 HLS segments in parallel
	cacheTTL        = 5 * time.Minute
)

type cacheEntry struct {
	data        []byte
	contentType string
	totalSize   int64 // -1 if unknown
	isPartial   bool
	headers     map[string]string
	createdAt   time.Time
	lastUsed    time.Time
}

type resolveEntry struct {
	URL       string            `json:"url"`
	Headers   map[string]string `json:"headers,omitempty"`
	ExpiresAt time.Time         `json:"expires_at"`
}

type StreamProxy struct {
	client *http.Client

	mu         sync.RWMutex
	cache      map[string]*cacheEntry
	cacheBytes int64
	inflight   map[string]chan struct{}

	rmu      sync.RWMutex
	resolved map[string]resolveEntry

	hits      atomic.Int64
	misses    atomic.Int64
	prefetches atomic.Int64
	bytesOut  atomic.Int64
}

func NewStreamProxy() *StreamProxy {
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   8 * time.Second,
			KeepAlive: 60 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          64,
		MaxIdleConnsPerHost:   16,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   8 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		TLSClientConfig:       &tls.Config{InsecureSkipVerify: false},
	}
	return &StreamProxy{
		client:   &http.Client{Transport: transport, Timeout: 0},
		cache:    make(map[string]*cacheEntry),
		inflight: make(map[string]chan struct{}),
		resolved: make(map[string]resolveEntry),
	}
}

func (p *StreamProxy) Stats() map[string]any {
	p.mu.RLock()
	entries := len(p.cache)
	cbytes := p.cacheBytes
	p.mu.RUnlock()
	p.rmu.RLock()
	rcount := len(p.resolved)
	p.rmu.RUnlock()
	return map[string]any{
		"cache_entries":  entries,
		"cache_bytes":    cbytes,
		"resolved_urls":  rcount,
		"hits":           p.hits.Load(),
		"misses":         p.misses.Load(),
		"prefetches":     p.prefetches.Load(),
		"bytes_streamed": p.bytesOut.Load(),
	}
}

func (p *StreamProxy) putCache(key string, entry *cacheEntry) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if old, ok := p.cache[key]; ok {
		p.cacheBytes -= int64(len(old.data))
	}
	entry.createdAt = time.Now()
	entry.lastUsed = entry.createdAt
	p.cache[key] = entry
	p.cacheBytes += int64(len(entry.data))

	// Evict oldest entries if over memory budget
	for p.cacheBytes > maxCacheBytes && len(p.cache) > 1 {
		var oldestKey string
		var oldestTime time.Time
		for k, v := range p.cache {
			if k == key {
				continue
			}
			if oldestKey == "" || v.lastUsed.Before(oldestTime) {
				oldestKey = k
				oldestTime = v.lastUsed
			}
		}
		if oldestKey == "" {
			break
		}
		p.cacheBytes -= int64(len(p.cache[oldestKey].data))
		delete(p.cache, oldestKey)
	}
}

func (p *StreamProxy) getCache(key string) *cacheEntry {
	p.mu.Lock()
	defer p.mu.Unlock()
	e, ok := p.cache[key]
	if !ok {
		return nil
	}
	if time.Since(e.createdAt) > cacheTTL {
		p.cacheBytes -= int64(len(e.data))
		delete(p.cache, key)
		return nil
	}
	e.lastUsed = time.Now()
	return e
}

// Prefetch warms either an HLS playlist (+ first N segments) or the first 2 MB of a VOD file.
func (p *StreamProxy) Prefetch(ctx context.Context, rawURL string, hdrs map[string]string) {
	if rawURL == "" || p.getCache(rawURL) != nil {
		return
	}
	p.mu.Lock()
	if _, busy := p.inflight[rawURL]; busy {
		p.mu.Unlock()
		return
	}
	done := make(chan struct{})
	p.inflight[rawURL] = done
	p.mu.Unlock()

	p.prefetches.Add(1)
	go func() {
		defer func() {
			p.mu.Lock()
			delete(p.inflight, rawURL)
			p.mu.Unlock()
			close(done)
		}()

		reqCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()

		isHLS := strings.Contains(strings.ToLower(rawURL), ".m3u8")
		req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, rawURL, nil)
		if err != nil {
			return
		}
		applyHeaders(req, hdrs)
		if !isHLS {
			req.Header.Set("Range", fmt.Sprintf("bytes=0-%d", prefetchBytes-1))
		}

		resp, err := p.client.Do(req)
		if err != nil {
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
			return
		}

		limit := int64(prefetchBytes)
		if isHLS {
			limit = 512 * 1024
		}
		data, err := io.ReadAll(io.LimitReader(resp.Body, limit))
		if err != nil || len(data) == 0 {
			return
		}

		totalSize := parseTotalSize(resp)
		p.putCache(rawURL, &cacheEntry{
			data:        data,
			contentType: resp.Header.Get("Content-Type"),
			totalSize:   totalSize,
			isPartial:   resp.StatusCode == http.StatusPartialContent || (totalSize > 0 && int64(len(data)) < totalSize),
			headers:     hdrs,
		})

		// If HLS playlist, concurrently prefetch the first few media segments
		if isHLS || bytes.HasPrefix(bytes.TrimSpace(data), []byte("#EXTM3U")) {
			segs := extractHLSSegments(rawURL, data, hlsPrefetchSegs)
			for _, segURL := range segs {
				p.prefetchSegment(context.Background(), segURL, hdrs)
			}
		}
	}()
}

func (p *StreamProxy) prefetchSegment(ctx context.Context, segURL string, hdrs map[string]string) {
	if p.getCache(segURL) != nil {
		return
	}
	go func() {
		reqCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
		defer cancel()
		req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, segURL, nil)
		if err != nil {
			return
		}
		applyHeaders(req, hdrs)
		resp, err := p.client.Do(req)
		if err != nil {
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return
		}
		data, err := io.ReadAll(io.LimitReader(resp.Body, 8*1024*1024))
		if err != nil || len(data) == 0 {
			return
		}
		p.putCache(segURL, &cacheEntry{
			data:        data,
			contentType: resp.Header.Get("Content-Type"),
			totalSize:   int64(len(data)),
			isPartial:   false,
			headers:     hdrs,
		})
	}()
}

// HandleProxy streams media to Kodi's C++ FFmpeg player with instant cache hits & full Range support.
func (p *StreamProxy) HandleProxy(w http.ResponseWriter, r *http.Request) {
	rawURL := r.URL.Query().Get("url")
	if rawURL == "" {
		http.Error(w, "missing url", http.StatusBadRequest)
		return
	}
	hdrs := parseQueryHeaders(r.URL.Query())

	// Wait briefly if a prefetch for this exact URL is already in flight
	p.mu.RLock()
	waitCh := p.inflight[rawURL]
	p.mu.RUnlock()
	if waitCh != nil {
		select {
		case <-waitCh:
		case <-time.After(1500 * time.Millisecond):
		case <-r.Context().Done():
			return
		}
	}

	if entry := p.getCache(rawURL); entry != nil {
		if p.serveFromCache(w, r, rawURL, entry, hdrs) {
			p.hits.Add(1)
			return
		}
	}

	p.misses.Add(1)
	p.streamUpstream(w, r, rawURL, hdrs)
}

func (p *StreamProxy) serveFromCache(w http.ResponseWriter, r *http.Request, rawURL string, entry *cacheEntry, hdrs map[string]string) bool {
	// Rewrite HLS playlists so every segment also routes through our fast local proxy
	isHLS := strings.Contains(strings.ToLower(rawURL), ".m3u8") || bytes.HasPrefix(bytes.TrimSpace(entry.data), []byte("#EXTM3U"))
	if isHLS {
		rewritten := rewriteHLSPlaylist(rawURL, entry.data, r.Host, hdrs)
		ct := entry.contentType
		if ct == "" {
			ct = "application/vnd.apple.mpegurl"
		}
		w.Header().Set("Content-Type", ct)
		w.Header().Set("Content-Length", strconv.Itoa(len(rewritten)))
		w.Header().Set("X-Stream-Core", "HIT-HLS")
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusOK)
			return true
		}
		w.WriteHeader(http.StatusOK)
		n, _ := w.Write(rewritten)
		p.bytesOut.Add(int64(n))
		return true
	}

	rangeHdr := r.Header.Get("Range")
	if rangeHdr != "" {
		start, end, ok := parseSingleRange(rangeHdr, entry.totalSize)
		if !ok {
			return false
		}
		// Only serve from cache if the requested slice is completely covered by our prefetched head
		if start < 0 || end < start || end >= int64(len(entry.data)) {
			return false
		}
		slice := entry.data[start : end+1]
		if entry.contentType != "" {
			w.Header().Set("Content-Type", entry.contentType)
		}
		w.Header().Set("Accept-Ranges", "bytes")
		if entry.totalSize > 0 {
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, entry.totalSize))
		} else {
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/*", start, end))
		}
		w.Header().Set("Content-Length", strconv.Itoa(len(slice)))
		w.Header().Set("X-Stream-Core", "HIT-RANGE")
		w.WriteHeader(http.StatusPartialContent)
		if r.Method != http.MethodHead {
			n, _ := w.Write(slice)
			p.bytesOut.Add(int64(n))
		}
		return true
	}

	// Non-range full request: only serve from cache if we have the entire object (e.g. HLS segment)
	if !entry.isPartial {
		if entry.contentType != "" {
			w.Header().Set("Content-Type", entry.contentType)
		}
		w.Header().Set("Accept-Ranges", "bytes")
		w.Header().Set("Content-Length", strconv.Itoa(len(entry.data)))
		w.Header().Set("X-Stream-Core", "HIT-FULL")
		w.WriteHeader(http.StatusOK)
		if r.Method != http.MethodHead {
			n, _ := w.Write(entry.data)
			p.bytesOut.Add(int64(n))
		}
		return true
	}
	return false
}

func (p *StreamProxy) streamUpstream(w http.ResponseWriter, r *http.Request, rawURL string, hdrs map[string]string) {
	req, err := http.NewRequestWithContext(r.Context(), r.Method, rawURL, nil)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	applyHeaders(req, hdrs)
	if rng := r.Header.Get("Range"); rng != "" {
		req.Header.Set("Range", rng)
	}

	resp, err := p.client.Do(req)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	isHLS := strings.Contains(strings.ToLower(rawURL), ".m3u8") ||
		strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "mpegurl")

	if isHLS && r.Method == http.MethodGet && resp.StatusCode == http.StatusOK {
		data, err := io.ReadAll(io.LimitReader(resp.Body, 512*1024))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		p.putCache(rawURL, &cacheEntry{
			data:        data,
			contentType: resp.Header.Get("Content-Type"),
			totalSize:   int64(len(data)),
			isPartial:   false,
			headers:     hdrs,
		})
		for _, segURL := range extractHLSSegments(rawURL, data, hlsPrefetchSegs) {
			p.prefetchSegment(context.Background(), segURL, hdrs)
		}
		rewritten := rewriteHLSPlaylist(rawURL, data, r.Host, hdrs)
		w.Header().Set("Content-Type", "application/vnd.apple.mpegurl")
		w.Header().Set("Content-Length", strconv.Itoa(len(rewritten)))
		w.Header().Set("X-Stream-Core", "MISS-HLS")
		w.WriteHeader(http.StatusOK)
		n, _ := w.Write(rewritten)
		p.bytesOut.Add(int64(n))
		return
	}

	for _, h := range []string{"Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"} {
		if v := resp.Header.Get(h); v != "" {
			w.Header().Set(h, v)
		}
	}
	w.Header().Set("X-Stream-Core", "STREAM")
	w.WriteHeader(resp.StatusCode)
	if r.Method == http.MethodHead {
		return
	}
	buf := make([]byte, 256*1024)
	n, _ := io.CopyBuffer(w, resp.Body, buf)
	p.bytesOut.Add(n)
}

func (p *StreamProxy) HandlePrefetch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		URL     string            `json:"url"`
		Headers map[string]string `json:"headers"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 64*1024)).Decode(&body); err != nil || body.URL == "" {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	p.Prefetch(context.Background(), body.URL, body.Headers)
	writeJSON(w, map[string]any{"ok": true, "proxy_url": p.BuildProxyURL(r.Host, body.URL, body.Headers)})
}

func (p *StreamProxy) HandleResolveCache(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		key := r.URL.Query().Get("key")
		p.rmu.RLock()
		entry, ok := p.resolved[key]
		p.rmu.RUnlock()
		if !ok || time.Now().After(entry.ExpiresAt) {
			w.WriteHeader(http.StatusNotFound)
			writeJSON(w, map[string]any{"found": false})
			return
		}
		writeJSON(w, map[string]any{
			"found":     true,
			"url":       entry.URL,
			"headers":   entry.Headers,
			"proxy_url": p.BuildProxyURL(r.Host, entry.URL, entry.Headers),
		})
	case http.MethodPost:
		var body struct {
			Key        string            `json:"key"`
			URL        string            `json:"url"`
			Headers    map[string]string `json:"headers"`
			TTLSeconds int               `json:"ttl_seconds"`
			Prefetch   bool              `json:"prefetch"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 64*1024)).Decode(&body); err != nil || body.Key == "" || body.URL == "" {
			http.Error(w, "invalid json", http.StatusBadRequest)
			return
		}
		ttl := time.Duration(body.TTLSeconds) * time.Second
		if ttl <= 0 {
			ttl = 20 * time.Minute
		}
		p.rmu.Lock()
		p.resolved[body.Key] = resolveEntry{
			URL:       body.URL,
			Headers:   body.Headers,
			ExpiresAt: time.Now().Add(ttl),
		}
		p.rmu.Unlock()
		if body.Prefetch {
			p.Prefetch(context.Background(), body.URL, body.Headers)
		}
		writeJSON(w, map[string]any{"ok": true, "proxy_url": p.BuildProxyURL(r.Host, body.URL, body.Headers)})
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (p *StreamProxy) BuildProxyURL(host, target string, hdrs map[string]string) string {
	if host == "" {
		host = "127.0.0.1:8775"
	}
	q := url.Values{}
	q.Set("url", target)
	if ua := hdrs["User-Agent"]; ua != "" {
		q.Set("ua", ua)
	}
	if ref := hdrs["Referer"]; ref != "" {
		q.Set("ref", ref)
	}
	if cookie := hdrs["Cookie"]; cookie != "" {
		q.Set("cookie", cookie)
	}
	return fmt.Sprintf("http://%s/proxy?%s", host, q.Encode())
}

func applyHeaders(req *http.Request, hdrs map[string]string) {
	req.Header.Set("User-Agent", "Mozilla/5.0 (Linux; Android 11; AFTKA) AppleWebKit/537.36 Chrome/120.0 Safari/537.36")
	for k, v := range hdrs {
		if k != "" && v != "" {
			req.Header.Set(k, v)
		}
	}
}

func parseQueryHeaders(q url.Values) map[string]string {
	h := map[string]string{}
	if ua := q.Get("ua"); ua != "" {
		h["User-Agent"] = ua
	}
	if ref := q.Get("ref"); ref != "" {
		h["Referer"] = ref
	}
	if c := q.Get("cookie"); c != "" {
		h["Cookie"] = c
	}
	return h
}

func parseTotalSize(resp *http.Response) int64 {
	if cr := resp.Header.Get("Content-Range"); cr != "" {
		if idx := strings.LastIndex(cr, "/"); idx != -1 {
			if n, err := strconv.ParseInt(cr[idx+1:], 10, 64); err == nil {
				return n
			}
		}
	}
	if resp.ContentLength > 0 {
		return resp.ContentLength
	}
	return -1
}

func parseSingleRange(hdr string, total int64) (int64, int64, bool) {
	if !strings.HasPrefix(hdr, "bytes=") {
		return 0, 0, false
	}
	spec := strings.TrimPrefix(hdr, "bytes=")
	if strings.Contains(spec, ",") {
		return 0, 0, false
	}
	parts := strings.SplitN(spec, "-", 2)
	if len(parts) != 2 || parts[0] == "" {
		return 0, 0, false
	}
	start, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return 0, 0, false
	}
	if parts[1] == "" {
		if total > 0 {
			return start, total - 1, true
		}
		return start, int64(prefetchBytes) - 1, true
	}
	end, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil {
		return 0, 0, false
	}
	return start, end, true
}

func resolveRef(baseStr, refStr string) string {
	base, err := url.Parse(baseStr)
	if err != nil {
		return refStr
	}
	ref, err := url.Parse(refStr)
	if err != nil {
		return refStr
	}
	return base.ResolveReference(ref).String()
}

func extractHLSSegments(baseURL string, data []byte, max int) []string {
	var out []string
	sc := bufio.NewScanner(bytes.NewReader(data))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		out = append(out, resolveRef(baseURL, line))
		if len(out) >= max {
			break
		}
	}
	return out
}

func rewriteHLSPlaylist(baseURL string, data []byte, host string, hdrs map[string]string) []byte {
	var out bytes.Buffer
	sc := bufio.NewScanner(bytes.NewReader(data))
	p := &StreamProxy{}
	for sc.Scan() {
		line := sc.Text()
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			out.WriteString(line)
			out.WriteByte('\n')
			continue
		}
		abs := resolveRef(baseURL, trimmed)
		out.WriteString(p.BuildProxyURL(host, abs, hdrs))
		out.WriteByte('\n')
	}
	return out.Bytes()
}
