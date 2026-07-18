// silo-usenet request router: fulfills Silo discover requests by making a title
// instantly streamable from usenet through the silo-usenet gateway. Silo has no
// video-source plugin capability, so the bytes still flow through the gateway +
// mount (the "sidecar"); this plugin is the brain that wires discover -> stream.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	goruntime "runtime"
	"strings"
	"time"

	pluginv1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
	publicmanifest "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/manifest"
	sdkruntime "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/runtime"
)

//go:embed manifest.json
var manifestJSON []byte

var httpClient = &http.Client{Timeout: 30 * time.Second}

// ---- gateway client -------------------------------------------------------

func gwCall(conn *pluginv1.RouterConnection, method, path string, body any) (map[string]any, int, error) {
	base := strings.TrimRight(conn.GetBaseUrl(), "/")
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, base+path, rdr)
	if err != nil {
		return nil, 0, err
	}
	if conn.GetApiKey() != "" {
		req.Header.Set("X-Api-Key", conn.GetApiKey())
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	return out, resp.StatusCode, nil
}

func folderKey(d *pluginv1.RequestDescriptor) string {
	if d.GetYear() > 0 {
		return fmt.Sprintf("%s (%d)", d.GetTitle(), d.GetYear())
	}
	return d.GetTitle()
}

func addBody(d *pluginv1.RequestDescriptor) map[string]any {
	ids := d.GetExternalIds()
	return map[string]any{
		"title":      d.GetTitle(),
		"year":       d.GetYear(),
		"media_type": d.GetMediaType(),
		"tmdb":       ids["tmdb"],
		"imdb":       ids["imdb"],
		"tvdb":       ids["tvdb"],
	}
}

// ---- capability servers ---------------------------------------------------

type runtimeServer struct {
	pluginv1.UnimplementedRuntimeServer
	manifest *pluginv1.PluginManifest
}

func (s *runtimeServer) GetManifest(context.Context, *pluginv1.GetManifestRequest) (*pluginv1.GetManifestResponse, error) {
	return &pluginv1.GetManifestResponse{Manifest: s.manifest}, nil
}

type routerServer struct {
	pluginv1.UnimplementedRequestRouterServer
}

func (r *routerServer) Fulfill(ctx context.Context, in *pluginv1.FulfillRequest) (*pluginv1.FulfillResponse, error) {
	conns := in.GetConnections()
	if len(conns) == 0 {
		return &pluginv1.FulfillResponse{Message: "no usenet connection configured"}, nil
	}
	conn := conns[0]
	d := in.GetRequest()
	mt := d.GetMediaType()
	if mt != "movie" && mt != "series" {
		return &pluginv1.FulfillResponse{Message: "unsupported media type"}, nil
	}

	out, code, err := gwCall(conn, http.MethodPost, "/add", addBody(d))
	if err != nil {
		return &pluginv1.FulfillResponse{Message: "gateway unreachable: " + err.Error()}, nil
	}
	if code != 200 || out["ok"] != true {
		msg, _ := out["message"].(string)
		if msg == "" {
			msg = fmt.Sprintf("gateway returned %d", code)
		}
		return &pluginv1.FulfillResponse{Message: msg}, nil
	}

	extID := folderKey(d)
	available, _ := out["available"].(bool)
	status := "downloading" // being made available (scanning into the library)
	if available {
		status = "completed"
	}

	qs := in.GetQualities()
	if len(qs) == 0 {
		qs = []*pluginv1.RequestedQuality{{}}
	}
	var targets []*pluginv1.FulfillmentTarget
	for _, q := range qs {
		targets = append(targets, &pluginv1.FulfillmentTarget{
			Quality:        q.GetId(),
			ConnectionId:   conn.GetId(),
			ExternalId:     extID,
			Status:         status,
			ExternalStatus: "streamable",
			Message:        "usenet on-demand",
		})
	}
	return &pluginv1.FulfillResponse{Targets: targets}, nil
}

func (r *routerServer) CheckStatus(ctx context.Context, in *pluginv1.CheckStatusRequest) (*pluginv1.CheckStatusResponse, error) {
	conns := in.GetConnections()
	var conn *pluginv1.RouterConnection
	if len(conns) > 0 {
		conn = conns[0]
	}
	d := in.GetRequest()
	var statuses []*pluginv1.TargetStatus
	for _, t := range in.GetTargets() {
		status := "downloading"
		if conn != nil {
			out, code, err := gwCall(conn, http.MethodPost, "/status", addBody(d))
			if err == nil && code == 200 {
				if avail, _ := out["available"].(bool); avail {
					status = "completed"
				}
			}
		}
		statuses = append(statuses, &pluginv1.TargetStatus{
			Quality:        t.GetQuality(),
			ConnectionId:   t.GetConnectionId(),
			Status:         status,
			ExternalStatus: "streamable",
		})
	}
	return &pluginv1.CheckStatusResponse{Statuses: statuses}, nil
}

func (r *routerServer) TestConnection(ctx context.Context, in *pluginv1.TestConnectionRequest) (*pluginv1.TestConnectionResponse, error) {
	conn := in.GetConnection()
	if conn == nil || conn.GetBaseUrl() == "" {
		return &pluginv1.TestConnectionResponse{Ok: false, Message: "base URL is required"}, nil
	}
	_, code, err := gwCall(conn, http.MethodGet, "/health", nil)
	if err != nil {
		return &pluginv1.TestConnectionResponse{Ok: false, Message: err.Error()}, nil
	}
	if code != 200 {
		return &pluginv1.TestConnectionResponse{Ok: false, Message: fmt.Sprintf("gateway returned %d", code)}, nil
	}
	return &pluginv1.TestConnectionResponse{Ok: true, Message: "gateway reachable"}, nil
}

func (r *routerServer) Validate(ctx context.Context, in *pluginv1.ValidateRequest) (*pluginv1.ValidateResponse, error) {
	fe := map[string]string{}
	if c := in.GetConnection(); c == nil || c.GetBaseUrl() == "" {
		fe["base_url"] = "gateway base URL is required"
	}
	return &pluginv1.ValidateResponse{FieldErrors: fe}, nil
}

func (r *routerServer) ListConfigOptions(ctx context.Context, in *pluginv1.ListConfigOptionsRequest) (*pluginv1.ListConfigOptionsResponse, error) {
	return &pluginv1.ListConfigOptionsResponse{OptionsByField: map[string]*pluginv1.ConfigOptionList{}}, nil
}

// ---- main -----------------------------------------------------------------

func main() {
	manifest, err := loadManifest()
	if err != nil {
		panic(err)
	}
	sdkruntime.Serve(sdkruntime.ServeConfig{
		Servers: sdkruntime.CapabilityServers{
			Runtime:       &runtimeServer{manifest: manifest},
			RequestRouter: &routerServer{},
		},
	})
}

func loadManifest() (*pluginv1.PluginManifest, error) {
	manifest, err := publicmanifest.Load(manifestJSON)
	if err != nil {
		return nil, fmt.Errorf("load embedded manifest: %w", err)
	}
	exe, err := os.Executable()
	if err != nil {
		return nil, fmt.Errorf("resolve executable path: %w", err)
	}
	data, err := os.ReadFile(exe)
	if err != nil {
		return nil, fmt.Errorf("read executable: %w", err)
	}
	sum := sha256.Sum256(data)
	manifest.Checksum = hex.EncodeToString(sum[:])
	if len(manifest.GetSupportedPlatforms()) == 0 {
		manifest.SupportedPlatforms = []*pluginv1.SupportedPlatform{
			{Os: goruntime.GOOS, Arch: goruntime.GOARCH},
		}
	}
	return manifest, nil
}
