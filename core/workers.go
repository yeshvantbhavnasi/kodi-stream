package main

import (
	"context"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

type TitleDoc struct {
	Name   string `json:"name"`
	URL    string `json:"url"`
	Parent string `json:"parent"`
	Group  string `json:"group"`
	Img    string `json:"img,omitempty"`
	Plot   string `json:"plot,omitempty"`
	Type   string `json:"t,omitempty"`
	Year   int    `json:"year,omitempty"`
}

type FeedVideo struct {
	VideoID   string `json:"video_id"`
	Title     string `json:"title"`
	Channel   string `json:"channel"`
	ChannelID string `json:"channel_id"`
	Published string `json:"published"`
	Thumb     string `json:"thumb"`
}

type BackgroundEngine struct {
	client *http.Client

	idxMu  sync.RWMutex
	titles []TitleDoc

	ytMu      sync.RWMutex
	feedCache map[string]cachedFeed
}

type cachedFeed struct {
	videos    []FeedVideo
	fetchedAt time.Time
}

func NewBackgroundEngine(client *http.Client) *BackgroundEngine {
	return &BackgroundEngine{
		client:    client,
		feedCache: make(map[string]cachedFeed),
	}
}

var yearRe = regexp.MustCompile(`\((\d{4})\)`)

func (e *BackgroundEngine) HandleIndexSync(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Titles []TitleDoc `json:"titles"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16*1024*1024)).Decode(&body); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	for i := range body.Titles {
		if body.Titles[i].Year == 0 {
			if m := yearRe.FindStringSubmatch(body.Titles[i].Name); len(m) == 2 {
				fmt.Sscanf(m[1], "%d", &body.Titles[i].Year)
			}
		}
	}
	e.idxMu.Lock()
	e.titles = body.Titles
	e.idxMu.Unlock()
	writeJSON(w, map[string]any{"ok": true, "indexed": len(body.Titles)})
}

func (e *BackgroundEngine) HandleSearch(w http.ResponseWriter, r *http.Request) {
	q := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("q")))
	if q == "" {
		writeJSON(w, map[string]any{"results": []TitleDoc{}})
		return
	}
	langsParam := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("langs")))
	var langOrder []string
	if langsParam != "" {
		for _, l := range strings.Split(langsParam, ",") {
			if s := strings.TrimSpace(l); s != "" {
				langOrder = append(langOrder, s)
			}
		}
	}
	words := strings.Fields(q)

	e.idxMu.RLock()
	docs := e.titles
	e.idxMu.RUnlock()

	type scored struct {
		doc      TitleDoc
		score    int
		langRank int
	}
	var hits []scored
	seen := make(map[string]struct{}, 64)

	for _, d := range docs {
		nameL := strings.ToLower(d.Name)
		textL := nameL + " " + strings.ToLower(d.Plot)
		matched := true
		for _, w := range words {
			if len(w) > 1 && !strings.Contains(textL, w) {
				matched = false
				break
			}
		}
		if !matched {
			continue
		}
		dedupKey := nameL + "|" + strings.ToLower(d.Group)
		if _, exists := seen[dedupKey]; exists {
			continue
		}
		seen[dedupKey] = struct{}{}

		base := strings.TrimSpace(yearRe.ReplaceAllString(nameL, ""))
		score := 4
		switch {
		case base == q:
			score = 0
		case strings.HasPrefix(nameL, q):
			score = 1
		case strings.Contains(nameL, " "+q):
			score = 2
		case strings.Contains(nameL, q):
			score = 3
		}

		langRank := len(langOrder)
		grpL := strings.ToLower(d.Group)
		for i, l := range langOrder {
			if strings.Contains(grpL, l) {
				langRank = i
				break
			}
		}
		hits = append(hits, scored{doc: d, score: score, langRank: langRank})
	}

	sort.Slice(hits, func(i, j int) bool {
		if hits[i].score != hits[j].score {
			return hits[i].score < hits[j].score
		}
		if hits[i].langRank != hits[j].langRank {
			return hits[i].langRank < hits[j].langRank
		}
		return hits[i].doc.Year > hits[j].doc.Year
	})

	limit := 60
	if len(hits) < limit {
		limit = len(hits)
	}
	out := make([]TitleDoc, limit)
	for i := 0; i < limit; i++ {
		out[i] = hits[i].doc
	}
	writeJSON(w, map[string]any{"results": out, "total": len(hits)})
}

type atomFeed struct {
	Title   string      `xml:"title"`
	Entries []atomEntry `xml:"entry"`
}

type atomEntry struct {
	VideoID   string `xml:"videoId"`
	Title     string `xml:"title"`
	Published string `xml:"published"`
}

func (e *BackgroundEngine) HandleYouTubeFeeds(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Channels []string `json:"channels"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 256*1024)).Decode(&body); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}

	results := make(map[string][]FeedVideo, len(body.Channels))
	var mu sync.Mutex
	var wg sync.WaitGroup
	sem := make(chan struct{}, 8)

	for _, cid := range body.Channels {
		cid := strings.TrimSpace(cid)
		if cid == "" {
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			vids := e.fetchChannelFeed(r.Context(), cid)
			mu.Lock()
			results[cid] = vids
			mu.Unlock()
		}()
	}
	wg.Wait()
	writeJSON(w, map[string]any{"feeds": results})
}

func (e *BackgroundEngine) fetchChannelFeed(ctx context.Context, channelID string) []FeedVideo {
	e.ytMu.RLock()
	if c, ok := e.feedCache[channelID]; ok && time.Since(c.fetchedAt) < 15*time.Minute {
		e.ytMu.RUnlock()
		return c.videos
	}
	e.ytMu.RUnlock()

	reqCtx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	feedURL := "https://www.youtube.com/feeds/videos.xml?channel_id=" + url.QueryEscape(channelID)
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, feedURL, nil)
	if err != nil {
		return nil
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (compatible; StreamCore/1.0)")
	resp, err := e.client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 512*1024))
	if err != nil {
		return nil
	}
	vids := parseYouTubeAtom(channelID, raw)
	e.ytMu.Lock()
	e.feedCache[channelID] = cachedFeed{videos: vids, fetchedAt: time.Now()}
	e.ytMu.Unlock()
	return vids
}

func parseYouTubeAtom(channelID string, raw []byte) []FeedVideo {
	var feed atomFeed
	if err := xml.Unmarshal(raw, &feed); err != nil {
		return nil
	}
	out := make([]FeedVideo, 0, len(feed.Entries))
	for _, entry := range feed.Entries {
		if entry.VideoID == "" {
			continue
		}
		out = append(out, FeedVideo{
			VideoID:   entry.VideoID,
			Title:     strings.TrimSpace(entry.Title),
			Channel:   strings.TrimSpace(feed.Title),
			ChannelID: channelID,
			Published: entry.Published,
			Thumb:     fmt.Sprintf("https://i.ytimg.com/vi/%s/hqdefault.jpg", entry.VideoID),
		})
	}
	return out
}

func (e *BackgroundEngine) Stats() map[string]any {
	e.idxMu.RLock()
	tCount := len(e.titles)
	e.idxMu.RUnlock()
	e.ytMu.RLock()
	fCount := len(e.feedCache)
	e.ytMu.RUnlock()
	return map[string]any{
		"indexed_titles": tCount,
		"cached_feeds":   fCount,
	}
}
