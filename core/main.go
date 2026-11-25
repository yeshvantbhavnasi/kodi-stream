package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func newServer(proxy *StreamProxy, engine *BackgroundEngine) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"ok": true, "version": "1.0.0"})
	})
	mux.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{
			"ok":     true,
			"proxy":  proxy.Stats(),
			"engine": engine.Stats(),
		})
	})
	mux.HandleFunc("/proxy", proxy.HandleProxy)
	mux.HandleFunc("/prefetch", proxy.HandlePrefetch)
	mux.HandleFunc("/resolve-cache", proxy.HandleResolveCache)
	mux.HandleFunc("/index/sync", engine.HandleIndexSync)
	mux.HandleFunc("/search", engine.HandleSearch)
	mux.HandleFunc("/youtube/feeds", engine.HandleYouTubeFeeds)
	return mux
}

func main() {
	addr := os.Getenv("STREAM_CORE_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8775"
	}
	proxy := NewStreamProxy()
	engine := NewBackgroundEngine(proxy.client)

	srv := &http.Server{
		Addr:              addr,
		Handler:           newServer(proxy, engine),
		ReadHeaderTimeout: 10 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("stream-core listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen error: %v", err)
		}
	}()

	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
