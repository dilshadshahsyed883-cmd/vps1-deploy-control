using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

static string Required(string key)
{
    var value = Environment.GetEnvironmentVariable(key);
    if (string.IsNullOrWhiteSpace(value)) throw new InvalidOperationException(key + " is required");
    return value;
}

var browserPassword = Required("REMOTE_BROWSER_PASSWORD");
var agentToken = Required("REMOTE_AGENT_TOKEN");
var publicOrigin = Required("REMOTE_PUBLIC_ORIGIN").TrimEnd('/');
if (browserPassword.Length < 20 || agentToken.Length < 32)
    throw new InvalidOperationException("Use long, random browser and agent credentials.");
if (!Uri.TryCreate(publicOrigin, UriKind.Absolute, out var originUri) || originUri.Scheme != "https" ||
    originUri.AbsolutePath != "/" || !string.IsNullOrEmpty(originUri.Query))
    throw new InvalidOperationException("REMOTE_PUBLIC_ORIGIN must be an HTTPS origin without a path.");

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
var sessions = new ConcurrentDictionary<string, DateTimeOffset>();
var attempts = new ConcurrentDictionary<string, (int Count, DateTimeOffset ResetAt)>();
var relay = new Relay();
app.Use(async (ctx, next) =>
{
    ctx.Response.Headers["X-Content-Type-Options"] = "nosniff";
    ctx.Response.Headers["X-Frame-Options"] = "DENY";
    ctx.Response.Headers["Referrer-Policy"] = "no-referrer";
    ctx.Response.Headers["Content-Security-Policy"] =
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self' wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'";
    ctx.Response.Headers["Cache-Control"] = "no-store";
    await next();
});
app.UseDefaultFiles();
app.UseStaticFiles();
app.UseWebSockets(new WebSocketOptions { KeepAliveInterval = TimeSpan.FromSeconds(20) });

bool HasSession(HttpContext ctx)
{
    if (!ctx.Request.Cookies.TryGetValue("remote-session", out var key)) return false;
    if (!sessions.TryGetValue(key, out var expiry)) return false;
    if (expiry > DateTimeOffset.UtcNow) return true;
    sessions.TryRemove(key, out _);
    return false;
}

bool FromOrigin(HttpContext ctx) =>
    string.Equals(ctx.Request.Headers.Origin.ToString(), publicOrigin, StringComparison.Ordinal);

app.MapGet("/healthz", () => Results.Ok(new { ok = true }));
app.MapPost("/api/login", async (HttpContext ctx) =>
{
    if (!FromOrigin(ctx)) return Results.StatusCode(403);
    var ip = ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown";
    var now = DateTimeOffset.UtcNow;
    var rate = attempts.AddOrUpdate(ip, (1, now.AddMinutes(15)), (_, state) =>
        state.ResetAt < now ? (1, now.AddMinutes(15)) : (state.Count + 1, state.ResetAt));
    if (rate.Count > 8) return Results.StatusCode(429);

    LoginRequest? request;
    try { request = await ctx.Request.ReadFromJsonAsync<LoginRequest>(); }
    catch (Exception) { return Results.BadRequest(); }

    if (request?.Password is null || !FixedEquals(request.Password, browserPassword))
        return Results.Unauthorized();

    attempts.TryRemove(ip, out _);
    var key = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
    sessions[key] = now.AddHours(8);
    ctx.Response.Cookies.Append("remote-session", key, new CookieOptions
    {
        HttpOnly = true, Secure = true, SameSite = SameSiteMode.Strict,
        Path = "/", MaxAge = TimeSpan.FromHours(8)
    });
    return Results.Ok(new { ok = true });
});

app.MapPost("/api/logout", (HttpContext ctx) =>
{
    if (!FromOrigin(ctx)) return Results.StatusCode(403);
    if (ctx.Request.Cookies.TryGetValue("remote-session", out var key))
        sessions.TryRemove(key, out _);
    ctx.Response.Cookies.Delete("remote-session", new CookieOptions
    { Path = "/", Secure = true, HttpOnly = true, SameSite = SameSiteMode.Strict });
    return Results.Ok(new { ok = true });
});

app.MapGet("/api/status", (HttpContext ctx) => HasSession(ctx)
    ? Results.Ok(new { online = relay.IsAgentOnline }) : Results.Unauthorized());

app.Map("/ws/agent", async (HttpContext ctx) =>
{
    var header = ctx.Request.Headers.Authorization.ToString();
    if (!ctx.WebSockets.IsWebSocketRequest || !header.StartsWith("Bearer ", StringComparison.Ordinal) ||
        !FixedEquals(header[7..], agentToken))
    { ctx.Response.StatusCode = 401; return; }

    using var socket = await ctx.WebSockets.AcceptWebSocketAsync();
    if (!relay.AcquireAgent(socket))
    {
        await socket.CloseAsync(WebSocketCloseStatus.PolicyViolation, "Agent already connected", CancellationToken.None);
        return;
    }
    await relay.SendViewerTextAsync("{\"type\":\"online\"}");
    try
    {
        while (socket.State == WebSocketState.Open)
        {
            var received = await ReadMessage(socket, 2_000_000);
            if (received is null) break;
            if (received.Value.Type == WebSocketMessageType.Binary)
                await relay.SendViewerBinaryAsync(received.Value.Data);
        }
    }
    catch (Exception ex) when (ex is WebSocketException or IOException or InvalidDataException or OperationCanceledException) { }
    finally
    {
        relay.ReleaseAgent(socket);
        await relay.SendViewerTextAsync("{\"type\":\"offline\"}");
    }
});

app.Map("/ws/viewer", async (HttpContext ctx) =>
{
    if (!ctx.WebSockets.IsWebSocketRequest || !HasSession(ctx) || !FromOrigin(ctx))
    { ctx.Response.StatusCode = 403; return; }

    using var socket = await ctx.WebSockets.AcceptWebSocketAsync();
    if (!relay.AcquireViewer(socket))
    {
        await socket.CloseAsync(WebSocketCloseStatus.PolicyViolation, "Viewer already connected", CancellationToken.None);
        return;
    }
    await relay.SendViewerTextAsync(relay.IsAgentOnline ? "{\"type\":\"online\"}" : "{\"type\":\"offline\"}");
    try
    {
        while (socket.State == WebSocketState.Open)
        {
            var received = await ReadMessage(socket, 4096);
            if (received is null) break;
            if (received.Value.Type != WebSocketMessageType.Text) continue;
            var json = Encoding.UTF8.GetString(received.Value.Data);
            if (ValidControl(json)) await relay.SendAgentTextAsync(json);
        }
    }
    catch (Exception ex) when (ex is WebSocketException or IOException or InvalidDataException or OperationCanceledException) { }
    finally { relay.ReleaseViewer(socket); }
});

app.Run();

static bool FixedEquals(string actual, string expected)
{
    var a = SHA256.HashData(Encoding.UTF8.GetBytes(actual));
    var b = SHA256.HashData(Encoding.UTF8.GetBytes(expected));
    return CryptographicOperations.FixedTimeEquals(a, b);
}

static async Task<(WebSocketMessageType Type, byte[] Data)?> ReadMessage(WebSocket socket, int limit)
{
    var buffer = new byte[64 * 1024];
    using var stream = new MemoryStream();
    WebSocketMessageType type = WebSocketMessageType.Text;
    while (true)
    {
        var r = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), CancellationToken.None);
        if (r.MessageType == WebSocketMessageType.Close) return null;
        if (stream.Length == 0) type = r.MessageType;
        if (r.MessageType != type || stream.Length + r.Count > limit)
            throw new InvalidDataException("Invalid/oversize WebSocket message");
        stream.Write(buffer, 0, r.Count);
        if (r.EndOfMessage) return (type, stream.ToArray());
    }
}

static bool ValidControl(string json)
{
    try
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (root.ValueKind != JsonValueKind.Object ||
            !root.TryGetProperty("type", out var typeElement)) return false;
        var type = typeElement.GetString();
        if (type == "mouse")
        {
            if (!root.TryGetProperty("action", out var action) ||
                !new[] { "move", "down", "up", "click", "double", "right" }.Contains(action.GetString()))
                return false;
            return Normalized(root, "x") && Normalized(root, "y");
        }
        if (type == "scroll")
            return root.TryGetProperty("delta", out var delta) &&
                delta.TryGetInt32(out var d) && d is >= -960 and <= 960;
        if (type == "text")
            return root.TryGetProperty("value", out var value) &&
                value.ValueKind == JsonValueKind.String && value.GetString()!.Length <= 256;
        if (type == "key")
            return root.TryGetProperty("value", out var key) &&
                new[] { "Enter", "Backspace", "Tab", "Escape", "ArrowUp", "ArrowDown",
                        "ArrowLeft", "ArrowRight", "Delete", "Home", "End", "PageUp",
                        "PageDown" }.Contains(key.GetString());
        if (type == "shortcut")
            return root.TryGetProperty("value", out var shortcut) &&
                new[] { "Ctrl+A", "Ctrl+C", "Ctrl+V", "Ctrl+Z" }.Contains(shortcut.GetString());
        return false;
    }
    catch (Exception) { return false; }
}
static bool Normalized(JsonElement obj, string property) =>
    obj.TryGetProperty(property, out var el) &&
    el.ValueKind == JsonValueKind.Number &&
    el.TryGetDouble(out var n) && double.IsFinite(n) && n >= 0 && n <= 1;

record LoginRequest(string Password);

sealed class Relay
{
    private readonly object gate = new();
    private readonly SemaphoreSlim viewerWrite = new(1, 1);
    private readonly SemaphoreSlim agentWrite = new(1, 1);
    private WebSocket? agent;
    private WebSocket? viewer;

    public bool IsAgentOnline { get { lock (gate) return agent?.State == WebSocketState.Open; } }
    public bool AcquireAgent(WebSocket socket)
    { lock (gate) { if (agent is not null) return false; agent = socket; return true; } }
    public bool AcquireViewer(WebSocket socket)
    { lock (gate) { if (viewer is not null) return false; viewer = socket; return true; } }
    public void ReleaseAgent(WebSocket socket)
    { lock (gate) { if (ReferenceEquals(agent, socket)) agent = null; } }
    public void ReleaseViewer(WebSocket socket)
    { lock (gate) { if (ReferenceEquals(viewer, socket)) viewer = null; } }
    public Task SendViewerTextAsync(string s) =>
        SendAsync(GetViewer(), viewerWrite, Encoding.UTF8.GetBytes(s), WebSocketMessageType.Text);
    public Task SendViewerBinaryAsync(byte[] jpeg) =>
        SendAsync(GetViewer(), viewerWrite, jpeg, WebSocketMessageType.Binary);
    public Task SendAgentTextAsync(string s) =>
        SendAsync(GetAgent(), agentWrite, Encoding.UTF8.GetBytes(s), WebSocketMessageType.Text);
    private WebSocket? GetAgent() { lock (gate) return agent; }
    private WebSocket? GetViewer() { lock (gate) return viewer; }
    private static async Task SendAsync(WebSocket? ws, SemaphoreSlim gate, byte[] data, WebSocketMessageType type)
    {
        if (ws is null) return;
        await gate.WaitAsync();
        try
        {
            if (ws.State == WebSocketState.Open)
                await ws.SendAsync(new ArraySegment<byte>(data), type, true, CancellationToken.None);
        }
        catch (Exception ex) when (ex is WebSocketException or ObjectDisposedException or IOException) { }
        finally { gate.Release(); }
    }
}
