// compose-scheduler dispatches an existing GitHub workflow from dedicated sidecars.
// It never merges pull requests or deploys containers itself.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
	_ "time/tzdata"
)

const (
	repository = "PintjesB/docker-compose"
	workflow = "nightly-renovate-merge.yml"
	branch = "master"
	healthPath = "/tmp/compose-scheduler-heartbeat"
	tokenPath = "/run/secrets/github_dispatch_token"
	pollInterval = 5 * time.Second
)

var brussels = mustLocation("Europe/Brussels")

func mustLocation(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil { panic(err) }
	return loc
}

type config struct {
	role string
	tokenFile string
	apiURL string
}

func readConfig() (config, error) {
	role := strings.ToLower(os.Getenv("SCHEDULER_ROLE"))
	if role != "primary" && role != "standby" {
		return config{}, errors.New("SCHEDULER_ROLE must be primary or standby")
	}
	tokenFile := os.Getenv("GITHUB_TOKEN_FILE")
	if tokenFile == "" { tokenFile = tokenPath }
	secret, err := os.ReadFile(tokenFile)
	if err != nil { return config{}, fmt.Errorf("read GitHub token file: %w", err) }
	if strings.TrimSpace(string(secret)) == "" {
		return config{}, errors.New("GitHub token file is empty")
	}
	return config{role: role, tokenFile: tokenFile, apiURL: "https://api.github.com"}, nil
}

// slot calculates the primary's Brussels-local slot for both processes.
// Primary acts at slot+5s, standby at slot+75s. No missed-slot catch-up.
func slot(now time.Time, role string) (time.Time, bool) {
	t := now.In(brussels)
	if t.Hour() != 3 && t.Hour() != 4 { return time.Time{}, false }
	if role == "primary" {
		if t.Minute()%5 != 2 || t.Second() < 5 { return time.Time{}, false }
		return time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), t.Minute(), 0, 0, brussels), true
	}
	if role == "standby" {
		if t.Minute()%5 != 3 || t.Second() < 15 { return time.Time{}, false }
		return time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), t.Minute()-1, 0, 0, brussels), true
	}
	return time.Time{}, false
}

func slotID(t time.Time) string { return t.Format(time.RFC3339) }
func runTitle(t time.Time) string { return "compose-scheduler " + slotID(t) }

type github struct {
	http *http.Client
	root string
	tokenFile string
}

func (g *github) request(ctx context.Context, method, path string, payload io.Reader) (*http.Response, error) {
	u, err := url.Parse(g.root + path)
	if err != nil { return nil, err }
	if u.Scheme != "https" && !(u.Scheme == "http" && strings.HasPrefix(u.Host, "127.0.0.1:")) {
		return nil, errors.New("GitHub API endpoint requires HTTPS")
	}
	req, err := http.NewRequestWithContext(ctx, method, u.String(), payload)
	if err != nil { return nil, err }
	secret, err := os.ReadFile(g.tokenFile)
	if err != nil { return nil, fmt.Errorf("read GitHub token file: %w", err) }
	token := strings.TrimSpace(string(secret))
	if token == "" { return nil, errors.New("GitHub token file is empty") }
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "pintjesb-compose-scheduler")
	if payload != nil { req.Header.Set("Content-Type", "application/json") }
	return g.http.Do(req)
}

func endpoint() string {
	return "/repos/" + repository + "/actions/workflows/" + workflow
}

// exists checks the exact slot title, not merely any recent workflow run.
func (g *github) exists(ctx context.Context, t time.Time) (bool, error) {
	path := endpoint() + "/runs?event=workflow_dispatch&branch=" + branch + "&per_page=100"
	resp, err := g.request(ctx, http.MethodGet, path, nil)
	if err != nil { return false, err }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("GitHub list workflow runs: HTTP %d", resp.StatusCode)
	}
	var data struct {
		Runs []struct {
			DisplayTitle string `json:"display_title"`
			Event string `json:"event"`
			HeadBranch string `json:"head_branch"`
		} `json:"workflow_runs"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&data); err != nil {
		return false, err
	}
	for _, run := range data.Runs {
		if run.DisplayTitle == runTitle(t) && run.Event == "workflow_dispatch" && run.HeadBranch == branch {
			return true, nil
		}
	}
	return false, nil
}

// The sole write operation is workflow_dispatch on the fixed trusted target.
func (g *github) dispatch(ctx context.Context, t time.Time) error {
	body, err := json.Marshal(map[string]any{
		"ref": branch,
		"inputs": map[string]string{
			"dry_run": "false",
			"scheduler_dispatch": "true",
			"schedule_slot": slotID(t),
		},
	})
	if err != nil { return err }
	resp, err := g.request(ctx, http.MethodPost, endpoint()+"/dispatches", strings.NewReader(string(body)))
	if err != nil { return err }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("GitHub dispatch: HTTP %d", resp.StatusCode)
	}
	return nil
}

type scheduler struct {
	cfg config
	api *github
	log *slog.Logger
	lastSlot string
	clock func() time.Time
}

func (s *scheduler) tick(ctx context.Context, now time.Time) {
	t, ready := slot(now, s.cfg.role)
	if !ready { return }
	id := slotID(t)
	if id == s.lastSlot { return }
	// Do not retry ambiguous POSTs in the same slot.
	// A restart rechecks GitHub, and standby checks independently.
	s.lastSlot = id
	ctx, cancel := context.WithTimeout(ctx, 25*time.Second)
	defer cancel()
	found, err := s.api.exists(ctx, t)
	if err != nil {
		s.log.Error("cannot verify slot; fail closed", "slot", id, "role", s.cfg.role, "error", err)
		return
	}
	if found {
		s.log.Info("slot already dispatched", "slot", id, "role", s.cfg.role)
		return
	}
	clock := time.Now
	if s.clock != nil { clock = s.clock }
	if current := clock().In(brussels); current.Hour() < 3 || current.Hour() >= 5 {
		s.log.Warn("dispatch skipped outside merge window", "slot", id)
		return
	}
	if err := s.api.dispatch(ctx, t); err != nil {
		s.log.Error("dispatch failed", "slot", id, "role", s.cfg.role, "error", err)
		return
	}
	s.log.Info("workflow dispatched", "slot", id, "role", s.cfg.role)
}

func writeHeartbeat() error {
	return os.WriteFile(healthPath, []byte("alive\n"), 0600)
}

func healthy() bool {
	st, err := os.Stat(healthPath)
	return err == nil && time.Since(st.ModTime()) >= 0 && time.Since(st.ModTime()) < 40*time.Second
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--healthcheck" {
		if !healthy() { os.Exit(1) }
		return
	}
	if len(os.Args) != 1 {
		fmt.Fprintln(os.Stderr, "usage: scheduler [--healthcheck]")
		os.Exit(2)
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg, err := readConfig()
	if err != nil {
		logger.Error("invalid scheduler configuration", "error", err)
		os.Exit(1)
	}
	logger.Info("scheduler started", "role", cfg.role, "timezone", "Europe/Brussels", "repository", repository)
	client := &http.Client{Timeout: 20*time.Second}
	s := &scheduler{cfg: cfg, api: &github{http: client, root: cfg.apiURL, tokenFile: cfg.tokenFile}, log: logger}
	if err := writeHeartbeat(); err != nil {
		logger.Error("heartbeat write failed", "error", err)
		os.Exit(1)
	}
	// Keep liveness independent of API latency. An API timeout is not a
	// reason to restart and risk sending an ambiguous dispatch twice.
	go func() {
		heartbeat := time.NewTicker(10 * time.Second)
		defer heartbeat.Stop()
		for range heartbeat.C {
			if err := writeHeartbeat(); err != nil {
				logger.Error("heartbeat write failed", "error", err)
				os.Exit(1)
			}
		}
	}()
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		s.tick(context.Background(), time.Now())
		<-ticker.C
	}
}
