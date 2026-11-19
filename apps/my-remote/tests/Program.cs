using System.Net.Http.Json;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20));
var token = timeout.Token;
var password = Environment.GetEnvironmentVariable("REMOTE_BROWSER_PASSWORD") ??
    throw new Exception("Missing browser credential.");
var agentToken = Environment.GetEnvironmentVariable("REMOTE_AGENT_TOKEN") ??
    throw new Exception("Missing agent credential.");
var publicOrigin = Environment.GetEnvironmentVariable("REMOTE_PUBLIC_ORIGIN") ??
    throw new Exception("Missing expected origin.");

using var http = new HttpClient { BaseAddress = new Uri("http://127.0.0.1:5123/") };
using var login = new HttpRequestMessage(HttpMethod.Post, "/api/login");
login.Headers.TryAddWithoutValidation("Origin", publicOrigin);
login.Content = JsonContent.Create(new { password });
using var response = await http.SendAsync(login, token);
Require(response.IsSuccessStatusCode, "Login failed.");
var cookie = response.Headers.GetValues("Set-Cookie")
    .First(v => v.StartsWith("remote-session=", StringComparison.Ordinal)).Split(';')[0];

using var agent = new ClientWebSocket();
agent.Options.SetRequestHeader("Authorization", "Bearer " + agentToken);
await agent.ConnectAsync(new Uri("ws://127.0.0.1:5123/ws/agent"), token);
using var viewer = new ClientWebSocket();
viewer.Options.SetRequestHeader("Origin", publicOrigin);
viewer.Options.SetRequestHeader("Cookie", cookie);
await viewer.ConnectAsync(new Uri("ws://127.0.0.1:5123/ws/viewer"), token);

var online = await Read(viewer, token);
Require(online.Type == WebSocketMessageType.Text &&
    Json(online.Data).GetProperty("type").GetString() == "online", "Viewer not informed that PC connected.");

var start = await Read(agent, token);
Require(start.Type == WebSocketMessageType.Text &&
    Json(start.Data).GetProperty("type").GetString() == "stream" &&
    Json(start.Data).GetProperty("enabled").GetBoolean(), "Viewer did not activate screen streaming.");

byte[] jpeg = [0xff, 0xd8, 0x00, 0x01, 0xff, 0xd9];
await agent.SendAsync(new ArraySegment<byte>(jpeg), WebSocketMessageType.Binary, true, token);
var frame = await Read(viewer, token);
Require(frame.Type == WebSocketMessageType.Binary && frame.Data.SequenceEqual(jpeg),
    "Screen frame not forwarded faithfully.");

var command = Encoding.UTF8.GetBytes("{\"type\":\"mouse\",\"action\":\"click\",\"x\":0.5,\"y\":0.25}");
await viewer.SendAsync(new ArraySegment<byte>(command), WebSocketMessageType.Text, true, token);
var forwarded = await Read(agent, token);
Require(forwarded.Type == WebSocketMessageType.Text &&
    Json(forwarded.Data).GetProperty("action").GetString() == "click", "Mouse control not forwarded.");

await viewer.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", token);
var stop = await Read(agent, token);
var release = await Read(agent, token);
Require(Json(stop.Data).GetProperty("type").GetString() == "stream" &&
    !Json(stop.Data).GetProperty("enabled").GetBoolean(), "Screen not stopped on viewer disconnect.");
Require(Json(release.Data).GetProperty("type").GetString() == "release",
    "Mouse was not released on viewer disconnect.");

Console.WriteLine("Authenticated WebSocket relay, streaming, controls, and disconnect cleanup: PASS.");

static JsonElement Json(byte[] data) => JsonDocument.Parse(data).RootElement.Clone();
static void Require(bool ok, string message)
{ if (!ok) throw new Exception(message); }

static async Task<(WebSocketMessageType Type, byte[] Data)> Read(ClientWebSocket socket, CancellationToken ct)
{
    var bytes = new byte[4096];
    using var stream = new MemoryStream();
    WebSocketReceiveResult part;
    do
    {
        part = await socket.ReceiveAsync(new ArraySegment<byte>(bytes), ct);
        if (part.MessageType == WebSocketMessageType.Close) throw new Exception("Unexpected close");
        stream.Write(bytes, 0, part.Count);
    } while (!part.EndOfMessage);
    return (part.MessageType, stream.ToArray());
}
