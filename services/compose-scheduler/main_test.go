package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func tm(text string) time.Time {
	t, err := time.Parse(time.RFC3339, text)
	if err != nil { panic(err) }
	return t
}

func TestSlotsCETCESTAndWindow(t *testing.T) {
	for _, tc := range []struct { when, role, expected string; ready bool }{
		{"2026-11-17T03:02:05+01:00", "primary", "2026-11-17T03:02:00+01:00", true},
		{"2026-11-17T03:02:04+01:00", "primary", "", false},
		{"2026-11-17T03:03:15+01:00", "standby", "2026-11-17T03:02:00+01:00", true},
		{"2026-11-17T04:58:15+01:00", "standby", "2026-11-17T04:57:00+01:00", true},
		{"2026-11-17T05:03:15+01:00", "standby", "", false},
		{"2026-11-17T02:02:05+01:00", "primary", "", false},
		{"2026-07-17T03:02:05+02:00", "primary", "2026-07-17T03:02:00+02:00", true},
		{"2026-07-17T03:03:15+02:00", "standby", "2026-07-17T03:02:00+02:00", true},
		{"2026-07-17T05:02:05+02:00", "primary", "", false},
		{"2026-07-17T04:57:05+02:00", "primary", "2026-07-17T04:57:00+02:00", true},
	} {
		t.Run(tc.when+"-"+tc.role,func(t *testing.T){
			got,ok := slot(tm(tc.when),tc.role,180)
			if ok!=tc.ready {t.Fatalf("ready=%t want %t",ok,tc.ready)}
			if ok && slotID(got)!=tc.expected {t.Fatalf("slot=%s want %s",slotID(got),tc.expected)}
		})
	}
}

func githubForTest(t *testing.T,handler http.HandlerFunc) (*github,func()) {
	t.Helper()
	server := httptest.NewServer(handler)
	file := filepath.Join(t.TempDir(),"token")
	if err := os.WriteFile(file,[]byte("test-token\n"),0600);err!=nil{t.Fatal(err)}
	return &github{http:server.Client(),root:server.URL,tokenFile:file},server.Close
}

func TestFixedAPIAndSafeDispatchPayload(t *testing.T) {
	target:=tm("2026-11-17T03:02:00+01:00")
	calls:=[]string{}
	g,closeServer:=githubForTest(t,func(w http.ResponseWriter,r *http.Request){
		calls=append(calls,r.Method+" "+r.URL.String())
		if r.Header.Get("Authorization")!="Bearer test-token" {t.Error("missing token")}
		if r.Method==http.MethodGet {
			fmt.Fprint(w,`{"workflow_runs":[{"display_title":"unrelated","event":"workflow_dispatch","head_branch":"master"}]}`)
			return
		}
		if r.Method!=http.MethodPost {t.Errorf("unexpected method %s",r.Method)}
		var data struct {Ref string `json:"ref"`;Inputs map[string]string `json:"inputs"`}
		if err:=json.NewDecoder(r.Body).Decode(&data);err!=nil {t.Error(err)}
		if data.Ref!="master" || len(data.Inputs)!=4 ||
			data.Inputs["dry_run"]!="false" ||
			data.Inputs["scheduler_dispatch"]!="true" ||
			data.Inputs["schedule_slot"]!=slotID(target) ||
			data.Inputs["schedule_start_time"]!="03:00" {
			t.Errorf("unsafe dispatch body: %+v",data)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	defer closeServer()
	exists,err:=g.exists(context.Background(),target)
	if err!=nil||exists {t.Fatalf("exists=%t error=%v",exists,err)}
	if err:=g.dispatch(context.Background(),target,180);err!=nil {t.Fatal(err)}
	if len(calls)!=2 || !strings.Contains(calls[0],endpoint()+"/runs?") ||
		!strings.Contains(calls[1],endpoint()+"/dispatches") {t.Errorf("calls=%v",calls)}
	for _,call:=range calls {if strings.Contains(call,"test-token") {t.Fatal("token leaked in URL")}}
}

func TestStandbySuppressesDuplicate(t *testing.T) {
	target:=tm("2026-11-17T03:02:00+01:00")
	posts:=0
	g,closeServer:=githubForTest(t,func(w http.ResponseWriter,r *http.Request){
		if r.Method==http.MethodPost {posts++;w.WriteHeader(http.StatusNoContent);return}
		fmt.Fprintf(w,`{"workflow_runs":[{"display_title":%q,"event":"workflow_dispatch","head_branch":"master"}]}`,runTitle(target))
	})
	defer closeServer()
	now:=tm("2026-11-17T03:03:15+01:00")
	s:=&scheduler{cfg:config{role:"standby",startMinute:180},api:g,
		log:slog.New(slog.NewJSONHandler(&bytes.Buffer{},nil)),
		clock:func()time.Time{return now}}
	s.tick(context.Background(),now)
	s.tick(context.Background(),now.Add(10*time.Second))
	if posts!=0 {t.Fatal("standby duplicated an existing primary run")}
	if s.lastSlot!=slotID(target) {t.Fatal("slot not recorded")}
}

func TestFailClosedOnGitHubError(t *testing.T) {
	posts:=0
	g,closeServer:=githubForTest(t,func(w http.ResponseWriter,r *http.Request){
		if r.Method==http.MethodPost {posts++;w.WriteHeader(http.StatusNoContent);return}
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	defer closeServer()
	now:=tm("2026-11-17T03:02:10+01:00")
	s:=&scheduler{cfg:config{role:"primary"},api:g,
		log:slog.New(slog.NewJSONHandler(&bytes.Buffer{},nil)),
		clock:func()time.Time{return now}}
	s.tick(context.Background(),now)
	s.tick(context.Background(),now.Add(5*time.Second))
	if posts!=0 {t.Fatal("must not dispatch without successful duplicate check")}
}

func TestMissingCredentialFailsBeforeNetwork(t *testing.T) {
	g:=&github{http:&http.Client{Timeout:time.Second},root:"https://api.github.com",
		tokenFile:filepath.Join(t.TempDir(),"missing")}
	if _,err:=g.exists(context.Background(),tm("2026-11-17T03:02:00+01:00"));err==nil {
		t.Fatal("missing secret accepted")
	}
}

func TestConfigurationRequiresSecretAndRole(t *testing.T) {
	t.Setenv("SCHEDULER_ROLE","invalid")
	if _,err:=readConfig();err==nil {t.Fatal("invalid role accepted")}
	t.Setenv("SCHEDULER_ROLE","primary")
	f:=filepath.Join(t.TempDir(),"credential")
	t.Setenv("GITHUB_TOKEN_FILE",f)
	if _,err:=readConfig();err==nil {t.Fatal("missing secret accepted")}
	if err:=os.WriteFile(f,[]byte("temporary-token"),0600);err!=nil{t.Fatal(err)}
	cfg,err:=readConfig()
	if err!=nil || cfg.tokenFile!=f || cfg.startMinute!=180 {t.Fatalf("cfg=%+v error=%v",cfg,err)}
}

func TestStandbyDispatchesWhenPrimaryAbsent(t *testing.T) {
	posts := 0
	g, closeServer := githubForTest(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			posts++
			w.WriteHeader(http.StatusNoContent)
			return
		}
		fmt.Fprint(w, `{"workflow_runs":[]}`)
	})
	defer closeServer()
	now := tm("2026-11-17T03:03:15+01:00")
	s := &scheduler{
		cfg: config{role: "standby",startMinute:180}, api: g,
		log: slog.New(slog.NewJSONHandler(&bytes.Buffer{}, nil)),
		clock: func() time.Time { return now },
	}
	s.tick(context.Background(), now)
	s.tick(context.Background(), now.Add(10*time.Second))
	if posts != 1 {
		t.Fatalf("expected exactly one failover dispatch, got %d", posts)
	}
}

func TestOutsideWindowNeverContactsGitHub(t *testing.T) {
	requests := 0
	g, closeServer := githubForTest(t, func(w http.ResponseWriter, r *http.Request) {
		requests++
		w.WriteHeader(http.StatusOK)
	})
	defer closeServer()
	now := tm("2026-11-17T05:02:10+01:00")
	s := &scheduler{
		cfg: config{role: "primary",startMinute:180}, api: g,
		log: slog.New(slog.NewJSONHandler(&bytes.Buffer{}, nil)),
		clock: func() time.Time { return now },
	}
	s.tick(context.Background(), now)
	if requests != 0 {
		t.Fatal("out-of-window scheduler contacted GitHub")
	}
}

func TestSmokeOnlyPrimaryWithinOneShotWindow(t *testing.T) {
	at := tm("2026-11-17T14:49:00+01:00")
	for _, tc := range []struct{ offset time.Duration; role string; want bool }{
		{-time.Second, "primary", false},
		{0, "primary", true},
		{5*time.Second, "primary", true},
		{44*time.Second, "primary", true},
		{45*time.Second, "primary", false},
		{5*time.Second, "standby", false},
		{5*time.Second, "disabled", false},
	} {
		got := smokeDue(at.Add(tc.offset), at, tc.role)
		if got != tc.want { t.Errorf("offset=%s role=%s: %v, want %v",tc.offset,tc.role,got,tc.want) }
	}
}

func TestSmokeDispatchCannotMerge(t *testing.T) {
	at := tm("2026-11-17T14:49:00+01:00")
	var calls int
	g,closeServer:=githubForTest(t,func(w http.ResponseWriter,r *http.Request) {
		calls++
		if r.Method != http.MethodPost || r.URL.Path != endpoint()+"/dispatches" {
			t.Errorf("unexpected request %s %s",r.Method,r.URL.Path)
		}
		var data struct { Ref string `json:"ref"`; Inputs map[string]string `json:"inputs"` }
		if err:=json.NewDecoder(r.Body).Decode(&data);err!=nil {t.Error(err)}
		if data.Ref!="master" || len(data.Inputs)!=3 ||
			data.Inputs["dry_run"]!="true" || data.Inputs["scheduler_dispatch"]!="false" ||
			data.Inputs["schedule_slot"]!="smoke-"+at.Format(time.RFC3339) {
			t.Errorf("unsafe smoke dispatch: %+v",data)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	defer closeServer()
	if err:=g.dispatchSmoke(context.Background(),at);err!=nil {t.Fatal(err)}
	if calls!=1 {t.Fatalf("want one dispatch, got %d",calls)}
}

func TestSmokeOnePerProcess(t *testing.T) {
	at:=tm("2026-11-17T14:49:00+01:00")
	posts:=0
	g,closeServer:=githubForTest(t,func(w http.ResponseWriter,r *http.Request){
		if r.Method==http.MethodPost {posts++}
		w.WriteHeader(http.StatusNoContent)
	})
	defer closeServer()
	s:=&scheduler{cfg:config{role:"primary",startMinute:180,smokeAt:at},api:g,
		log:slog.New(slog.NewJSONHandler(&bytes.Buffer{},nil))}
	s.tick(context.Background(),at)
	s.tick(context.Background(),at.Add(5*time.Second))
	if posts!=1 {t.Fatalf("expected exactly one smoke dispatch, got %d",posts)}
}

func TestStartTimeParsing(t *testing.T) {
	for _,tc:=range []struct{raw string; want int}{
		{"",180},{"03:00",180},{"00:00",0},{"23:59",1439},{"21:15",1275},{"05:07",307},
	} {
		got,err:=parseStartTime(tc.raw)
		if err!=nil || got!=tc.want {t.Errorf("%q: got %d error %v; want %d",tc.raw,got,err,tc.want)}
	}
	for _,raw:=range []string{"3:00","03:0","24:00","01:60","bad"," 03:00","03:00 ","-1:00","03:99"} {
		if _,err:=parseStartTime(raw);err==nil {t.Errorf("invalid start accepted: %q",raw)}
	}
}

func TestCustomScheduleSlotsAcrossMidnight(t *testing.T) {
	for _,tc:=range []struct{when,role string; start int; want string}{
		{"2026-11-17T21:17:05+01:00","primary",1275,"2026-11-17T21:17:00+01:00"},
		{"2026-11-17T21:18:15+01:00","standby",1275,"2026-11-17T21:17:00+01:00"},
		{"2026-11-18T00:02:05+01:00","primary",1410,"2026-11-18T00:02:00+01:00"},
		{"2026-11-18T00:03:15+01:00","standby",1410,"2026-11-18T00:02:00+01:00"},
		{"2026-11-18T01:32:05+01:00","primary",1410,""},
		{"2026-07-18T00:02:05+02:00","primary",1410,"2026-07-18T00:02:00+02:00"},
	} {
		got,ok:=slot(tm(tc.when),tc.role,tc.start)
		if (tc.want=="")!=(!ok) || (ok && slotID(got)!=tc.want) {
			t.Errorf("when=%s role=%s start=%d slot=%s ready=%t want=%s",tc.when,tc.role,tc.start,slotID(got),ok,tc.want)
		}
	}
}

func TestReadConfigStartTime(t *testing.T) {
	file:=filepath.Join(t.TempDir(),"token")
	if err:=os.WriteFile(file,[]byte("token"),0600);err!=nil {t.Fatal(err)}
	t.Setenv("SCHEDULER_ROLE","primary")
	t.Setenv("GITHUB_TOKEN_FILE",file)
	t.Setenv("SCHEDULER_START_TIME","21:15")
	cfg,err:=readConfig()
	if err!=nil || cfg.startMinute!=1275 {t.Fatalf("cfg=%+v error=%v",cfg,err)}
	t.Setenv("SCHEDULER_START_TIME","25:00")
	if _,err:=readConfig();err==nil {t.Fatal("invalid start time accepted")}
}
