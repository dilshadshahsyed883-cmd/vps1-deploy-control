using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows.Forms;

internal static class NativeInput
{
    private const uint InputMouse = 0, InputKeyboard = 1;
    private const uint MouseLeftDown = 0x0002, MouseLeftUp = 0x0004;
    private const uint MouseRightDown = 0x0008, MouseRightUp = 0x0010;
    private const uint MouseWheel = 0x0800;
    private const uint KeyUp = 0x0002, KeyUnicode = 0x0004;

    [DllImport("user32.dll")] private static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint count, [In] INPUT[] inputs, int size);

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT { public uint Type; public InputUnion Union; }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)] public MOUSEINPUT Mouse;
        [FieldOffset(0)] public KEYBDINPUT Keyboard;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT
    {
        public int Dx;
        public int Dy;
        public uint MouseData;
        public uint Flags;
        public uint Time;
        public UIntPtr ExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort VirtualKey;
        public ushort ScanCode;
        public uint Flags;
        public uint Time;
        public UIntPtr ExtraInfo;
    }

    private static void Push(params INPUT[] inputs)
    {
        if (inputs.Length > 0) SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>());
    }
    private static INPUT Mouse(uint flags, uint data = 0) => new()
    { Type = InputMouse, Union = new InputUnion { Mouse = new MOUSEINPUT { Flags = flags, MouseData = data } } };
    private static INPUT Keyboard(ushort key, bool up = false) => new()
    { Type = InputKeyboard, Union = new InputUnion { Keyboard = new KEYBDINPUT { VirtualKey = key, Flags = up ? KeyUp : 0 } } };
    private static INPUT Unicode(char value, bool up) => new()
    { Type = InputKeyboard, Union = new InputUnion { Keyboard = new KEYBDINPUT {
        ScanCode = value, Flags = KeyUnicode | (up ? KeyUp : 0) } } };
    private static void Tap(uint down, uint up) => Push(Mouse(down), Mouse(up));

    public static void ReleaseLeftButton() => Push(Mouse(MouseLeftUp));

    public static void Apply(JsonElement message)
    {
        var type = message.GetProperty("type").GetString();
        if (type == "release") { ReleaseLeftButton(); return; } // server-only disconnect event
        switch (type)
        {
            case "mouse":
                var bounds = Screen.PrimaryScreen?.Bounds;
                if (bounds is null) return;
                var x = message.GetProperty("x").GetDouble();
                var y = message.GetProperty("y").GetDouble();
                if (!double.IsFinite(x) || !double.IsFinite(y) || x < 0 || y < 0 || x > 1 || y > 1) return;
                SetCursorPos(bounds.Value.Left + (int)Math.Round(x * (bounds.Value.Width - 1)),
                    bounds.Value.Top + (int)Math.Round(y * (bounds.Value.Height - 1)));
                switch (message.GetProperty("action").GetString())
                {
                    case "down": Push(Mouse(MouseLeftDown)); break;
                    case "up": ReleaseLeftButton(); break;
                    case "click": Tap(MouseLeftDown, MouseLeftUp); break;
                    case "double": Tap(MouseLeftDown, MouseLeftUp); Tap(MouseLeftDown, MouseLeftUp); break;
                    case "right": Tap(MouseRightDown, MouseRightUp); break;
                }
                break;
            case "scroll":
                var delta = message.GetProperty("delta").GetInt32();
                if (delta is >= -960 and <= 960) Push(Mouse(MouseWheel, unchecked((uint)delta)));
                break;
            case "text":
                var text = message.GetProperty("value").GetString() ?? "";
                if (text.Length > 256) return;
                foreach (var c in text) Push(Unicode(c, false), Unicode(c, true));
                break;
            case "key":
                var key = message.GetProperty("value").GetString();
                if (KeyMap.TryGetValue(key ?? "", out var code)) Push(Keyboard(code), Keyboard(code, true));
                break;
            case "shortcut":
                var shortcut = message.GetProperty("value").GetString();
                var letter = shortcut switch { "Ctrl+A" => 0x41, "Ctrl+C" => 0x43,
                    "Ctrl+V" => 0x56, "Ctrl+Z" => 0x5A, _ => 0 };
                if (letter != 0) Push(Keyboard(0x11), Keyboard((ushort)letter),
                    Keyboard((ushort)letter, true), Keyboard(0x11, true));
                break;
        }
    }

    private static readonly Dictionary<string, ushort> KeyMap = new()
    {
        ["Enter"] = 0x0D, ["Backspace"] = 0x08, ["Tab"] = 0x09,
        ["Escape"] = 0x1B, ["ArrowUp"] = 0x26, ["ArrowDown"] = 0x28,
        ["ArrowLeft"] = 0x25, ["ArrowRight"] = 0x27, ["Delete"] = 0x2E,
        ["Home"] = 0x24, ["End"] = 0x23, ["PageUp"] = 0x21, ["PageDown"] = 0x22
    };
}
