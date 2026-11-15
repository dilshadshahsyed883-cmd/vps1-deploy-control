using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Windows.Forms;

internal static class Program
{
    private static volatile bool streaming;
    [STAThread]
    private static async Task Main()
    {
        var baseUrl = Environment.GetEnvironmentVariable("REMOTE_SERVER_URL");
        var token = Environment.GetEnvironmentVariable("REMOTE_AGENT_TOKEN");
        if (string.IsNullOrWhiteSpace(baseUrl) || string.IsNullOrWhiteSpace(token))
        {
            Console.Error.WriteLine("Set REMOTE_SERVER_URL (wss://host) and REMOTE_AGENT_TOKEN.");
            return;
        }
        if (!Uri.TryCreate(baseUrl.TrimEnd('/') + "/ws/agent", UriKind.Absolute, out var endpoint)
            || endpoint.Scheme != "wss")
        {
            Console.Error.WriteLine("HTTPS/WSS connection is required. Server URL must begin wss://.");
            return;
        }

        using var stop = new CancellationTokenSource();
        Console.CancelKeyPress += (_, args) => { args.Cancel = true; stop.Cancel(); };
        Console.WriteLine("My Remote agent: outbound connection only; Ctrl+C stops.");
        while (!stop.IsCancellationRequested)
        {
            try
            {
                using var socket = new ClientWebSocket();
                socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
                socket.Options.SetRequestHeader("Authorization", "Bearer " + token);
                Console.WriteLine("Connecting...");
                await socket.ConnectAsync(endpoint, stop.Token);
                streaming = false;
                Console.WriteLine("Connected. Waiting for an authenticated viewer.");
                using var connection = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
                var receiver = ReceiveControls(socket, connection.Token);
                try
                {
                    while (socket.State == WebSocketState.Open && !connection.IsCancellationRequested)
                    {
                        if (streaming)
                        {
                            var jpeg = CaptureJpeg();
                            await socket.SendAsync(new ArraySegment<byte>(jpeg), WebSocketMessageType.Binary, true, connection.Token);
                        }
                        await Task.Delay(250, connection.Token); // max ~4fps only while viewing
                    }
                }
                finally
                {
                    connection.Cancel();
                    try { await receiver; }
                    catch (Exception ex) when (ex is OperationCanceledException or WebSocketException or IOException) { }
                    finally { streaming = false; NativeInput.ReleaseLeftButton(); }
                }
            }
            catch (Exception ex) when (ex is WebSocketException or HttpRequestException or IOException
                or OperationCanceledException or InvalidOperationException)
            {
                if (!stop.IsCancellationRequested) Console.Error.WriteLine("Disconnected: " + ex.Message);
            }
            if (!stop.IsCancellationRequested)
            {
                try { await Task.Delay(TimeSpan.FromSeconds(5), stop.Token); }
                catch (OperationCanceledException) { }
            }
        }
        Console.WriteLine("Agent stopped.");
    }

    private static byte[] CaptureJpeg()
    {
        var bounds = Screen.PrimaryScreen?.Bounds
            ?? throw new InvalidOperationException("No interactive display detected.");
        if (bounds.Width < 1 || bounds.Height < 1)
            throw new InvalidOperationException("Invalid display size.");
        var scale = Math.Min(1.0, 1280.0 / bounds.Width);
        var width = Math.Max(1, (int)Math.Round(bounds.Width * scale));
        var height = Math.Max(1, (int)Math.Round(bounds.Height * scale));
        using var source = new Bitmap(bounds.Width, bounds.Height);
        using (var g = Graphics.FromImage(source))
            g.CopyFromScreen(bounds.Left, bounds.Top, 0, 0, bounds.Size, CopyPixelOperation.SourceCopy);
        using var compressed = new Bitmap(width, height);
        using (var g = Graphics.FromImage(compressed))
        {
            g.InterpolationMode = InterpolationMode.HighQualityBilinear;
            g.DrawImage(source, 0, 0, width, height);
        }
        using var output = new MemoryStream();
        var codec = ImageCodecInfo.GetImageEncoders().First(i => i.MimeType == "image/jpeg");
        using var options = new EncoderParameters(1);
        options.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 55L);
        compressed.Save(output, codec, options);
        return output.ToArray();
    }

    private static async Task ReceiveControls(ClientWebSocket socket, CancellationToken stop)
    {
        var buffer = new byte[4096];
        while (socket.State == WebSocketState.Open && !stop.IsCancellationRequested)
        {
            using var ms = new MemoryStream();
            WebSocketReceiveResult result;
            do
            {
                result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), stop);
                if (result.MessageType == WebSocketMessageType.Close) return;
                if (ms.Length + result.Count > 4096) return;
                ms.Write(buffer, 0, result.Count);
            } while (!result.EndOfMessage);
            if (result.MessageType != WebSocketMessageType.Text) continue;
            try
            {
                using var doc = JsonDocument.Parse(ms.ToArray());
                var root = doc.RootElement;
                if (root.TryGetProperty("type", out var kind) && kind.GetString() == "stream")
                {
                    streaming = root.GetProperty("enabled").GetBoolean();
                    continue;
                }
                NativeInput.Apply(root);
            }
            catch (Exception ex) when (ex is JsonException or ArgumentException or InvalidOperationException)
            {
                Console.Error.WriteLine("Invalid control packet ignored.");
            }
        }
    }
}
