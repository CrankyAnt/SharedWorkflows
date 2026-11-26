// Read a live Kernel-File ETW session and write each event's process id, parent process id, operation and path to a
// neutral event file, one tab-separated line per event in stream order. The recording step's check classifies the file.
//
//   dotnet EtwConsumer.dll <session> <event file> <ready file> <stop file> <path prefix> ...
//
// Operations: start (a process start; the path is its image, and a fifth column holds its command line with
// backslashes, tabs and line breaks escaped; a start written before its command line came has no fifth column), stop
// (a process stop; its path is empty), open and opendir (a successful open of an existing file or directory), openattr
// (a successful attribute-only open of an existing path: one without FILE_DIRECTORY_FILE and FILE_NON_DIRECTORY_FILE,
// with FILE_OPEN_REPARSE_POINT, as CPython's stat makes it), write (a successful create or overwrite open on a DOS
// volume or under a path prefix; a FILE_OPEN_IF open under a path prefix that opened an existing path also writes its
// read operation), absent (a read open of a missing path or under a non-directory), enum and enum-absent (a directory
// enumeration; the path is the directory, a backslash and the queried pattern). Read events count only under a path
// prefix, an NT device path; write events also count under any DOS drive's NT device. A process's repeated event is
// written once, except an enumeration (the first enumeration of each open of a directory is written every time, so
// each listing is one line) and a stop.
using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Parsers.Kernel;
using Microsoft.Diagnostics.Tracing.Session;

public static unsafe class Program
{
    public static readonly Guid KernelFile = new Guid("edd08927-9cc4-4e65-b970-c2560fb5c289");
    public static readonly Guid KernelProcess = new Guid("22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716");
    const ulong FileKeywords = 0xF0;  // file names, FILEIO (directory enumerations), operation ends, creates
    const ulong ProcessKeywords = 0x10;

    static EventProcessor processor;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    static extern uint QueryDosDeviceW(string deviceName, char[] targetPath, int max);

    static string[] DosDevicePrefixes()
    {
        var list = new List<string>();
        var buffer = new char[1024];
        for (char letter = 'A'; letter <= 'Z'; letter++)
        {
            uint count = QueryDosDeviceW(letter + ":", buffer, buffer.Length);
            if (count == 0) continue;
            int end = Array.IndexOf(buffer, '\0');
            string target = new string(buffer, 0, end < 0 ? (int)count : end).TrimEnd('\\');
            if (target.Length > 0) list.Add(target);
        }
        return list.ToArray();
    }

    static int Main(string[] args)
    {
        if (args.Length < 5)
        {
            Console.Error.WriteLine("usage: EtwConsumer <session> <event file> <ready file> <stop file> <path prefix> ...");
            return 2;
        }
        var prefixes = new string[args.Length - 4];
        for (int index = 4; index < args.Length; index++) prefixes[index - 4] = args[index].TrimEnd('\\');
        var writer = new StreamWriter(args[1], false, new UTF8Encoding(false), 1 << 20) { NewLine = "\n" };
        processor = new EventProcessor(writer, prefixes, DosDevicePrefixes());
        long lost;
        long drained;
        try
        {
            using (var session = new TraceEventSession(args[0], TraceEventSessionOptions.Create))
            {
                session.BufferSizeMB = 256;
                // The kernel process events carry the command line, which the Kernel-Process start lacks.
                session.EnableKernelProvider(KernelTraceEventParser.Keywords.Process);
                session.Source.Kernel.ProcessStart += CommandLine;
                session.Source.AllEvents += OnEvent;
                session.EnableProvider(KernelProcess, TraceEventLevel.Verbose, ProcessKeywords);
                session.EnableProvider(KernelFile, TraceEventLevel.Verbose, FileKeywords);
                var worker = new Thread(() => session.Source.Process());
                worker.Start();
                File.WriteAllText(args[2], "ready\n");
                while (!File.Exists(args[3])) Thread.Sleep(20);
                // The session counts the events it dropped while it runs; once stopped, it no longer answers.
                long dropped = session.EventsLost;
                var stopping = Stopwatch.StartNew();
                session.Stop();
                worker.Join();
                drained = stopping.ElapsedMilliseconds;
                lost = dropped;
                try { lost = Math.Max(dropped, session.Source.EventsLost); }
                catch (Exception) { }
            }
        }
        finally
        {
            processor.FlushAll();
            writer.Dispose();
        }
        Console.WriteLine("drained_ms=" + drained + " events=" + processor.Events + " lines=" + processor.Lines + " lost=" + lost);
        return 0;
    }

    static void OnEvent(TraceEvent data)
    {
        if (data.DataStart == IntPtr.Zero || data.EventDataLength < 0) return;
        processor.ProcessEvent(data.ProviderGuid, (int)data.ID, (byte)data.Version, (uint)data.ProcessID,
            new ReadOnlySpan<byte>((void*)data.DataStart, data.EventDataLength));
    }

    static void CommandLine(ProcessTraceData data) =>
        processor.ProcessCommandLine((uint)data.ProcessID, (uint)data.ParentID, data.CommandLine);
}

public sealed class EventProcessor
{
    const uint FileOpen = 1;  // the create disposition of an open that neither creates nor overwrites
    const uint FileOpenIf = 3;  // open existing or create new without truncating
    const uint FileOverwriteIf = 5;  // highest valid NT CreateDisposition
    const ulong FileOpened = 1;  // IoStatus.Information when an existing path was opened
    const uint DirectoryFile = 0x1;
    const uint NonDirectoryFile = 0x40;
    const uint OpenReparsePoint = 0x200000;

    struct Pending
    {
        public uint Pid;
        public string Operation;
        public string ReadOperation;  // for FILE_OPEN_IF under a kept prefix: also emit a read when FILE_OPENED
        public string Path;
        public bool EachTime;  // a listing, written however often the process lists the directory
        public bool IsWrite;
    }

    struct StartInfo
    {
        public uint Parent;
        public string Value;
    }

    readonly TextWriter writer;
    readonly string[] prefixes;
    readonly string[] writePrefixes;
    readonly Dictionary<ulong, string> nameByObject = new Dictionary<ulong, string>();
    readonly Dictionary<ulong, string> nameByKey = new Dictionary<ulong, string>();
    readonly Dictionary<ulong, Pending> pending = new Dictionary<ulong, Pending>();
    readonly Dictionary<uint, uint> parentOf = new Dictionary<uint, uint>();
    readonly Dictionary<uint, ulong> sequenceOf = new Dictionary<uint, ulong>();
    readonly Dictionary<uint, HashSet<string>> written = new Dictionary<uint, HashSet<string>>();
    readonly HashSet<ulong> enumerated = new HashSet<ulong>();  // the file objects enumerated since their open
    // A process start comes as two events, the Kernel-Process start with the image and the kernel process start with the
    // command line, in either order; the first one waits here for the second, paired by pid and parent pid.
    readonly Dictionary<uint, StartInfo> imageOf = new Dictionary<uint, StartInfo>();
    readonly Dictionary<uint, StartInfo> commandLineOf = new Dictionary<uint, StartInfo>();
    readonly Dictionary<uint, uint> startedWithoutCommandLine = new Dictionary<uint, uint>();

    public long Events { get; private set; }
    public long Lines { get; private set; }

    public EventProcessor(TextWriter writer, string[] prefixes, string[] writePrefixes)
    {
        this.writer = writer;
        this.prefixes = Trim(prefixes);
        this.writePrefixes = Trim(writePrefixes);
    }

    static string[] Trim(string[] items)
    {
        if (items == null) return Array.Empty<string>();
        var result = new string[items.Length];
        for (int index = 0; index < items.Length; index++) result[index] = (items[index] ?? "").TrimEnd('\\');
        return result;
    }

    static ulong ReadUInt64(ReadOnlySpan<byte> payload, int offset) =>
        BinaryPrimitives.ReadUInt64LittleEndian(payload.Slice(offset, 8));

    static uint ReadUInt32(ReadOnlySpan<byte> payload, int offset) =>
        BinaryPrimitives.ReadUInt32LittleEndian(payload.Slice(offset, 4));

    static string Text(ReadOnlySpan<byte> payload, int offset)
    {
        if (offset < 0 || offset >= payload.Length) return "";
        int byteCount = (payload.Length - offset) & ~1;
        if (byteCount <= 0) return "";
        ReadOnlySpan<byte> slice = payload.Slice(offset, byteCount);
        int end = 0;
        while (end + 1 < slice.Length && (slice[end] != 0 || slice[end + 1] != 0)) end += 2;
        return end == 0 ? "" : Encoding.Unicode.GetString(slice.Slice(0, end));
    }

    static bool Matches(string path, string[] roots)
    {
        if (path.Length == 0) return false;
        foreach (string prefix in roots)
        {
            if (prefix.Length > 0 && path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
                && (path.Length == prefix.Length || path[prefix.Length] == '\\'))
                return true;
        }
        return false;
    }

    bool Kept(string path) => Matches(path, prefixes);

    bool KeptWrite(string path) => Matches(path, writePrefixes);

    // STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND, STATUS_NO_SUCH_FILE, STATUS_NOT_A_DIRECTORY
    static bool NotFound(uint status) => status == 0xC0000034 || status == 0xC000003A || status == 0xC000000F || status == 0xC0000103;

    // An open that sets neither directory option and opens a reparse point itself is an attribute-only open, as
    // CPython's stat makes it. Share mode 0 alone does not downgrade an open, since an exclusive-share content read
    // with backup semantics sets neither directory option.
    static string OpenOperation(uint options)
    {
        if ((options & DirectoryFile) != 0) return "opendir";
        if ((options & NonDirectoryFile) == 0 && (options & OpenReparsePoint) != 0) return "openattr";
        return "open";
    }

    public void ProcessEvent(Guid provider, int id, byte version, uint pid, ReadOnlySpan<byte> payload)
    {
        Events++;
        if (provider == Program.KernelProcess)
        {
            if (id == 1 && payload.Length >= 56) Started(version, payload);
            else if (id == 2 && payload.Length >= 4) Stopped(version, payload);
            return;
        }
        if (provider != Program.KernelFile) return;
        switch (id)
        {
            case 10:
            {
                if (payload.Length < 8) break;
                string name = Text(payload, 8);
                if (Kept(name)) nameByKey[ReadUInt64(payload, 0)] = name;
                break;
            }
            case 12:
            {
                int nameOffset = version >= 1 ? 32 : 28;
                if (payload.Length < nameOffset) break;
                ulong irp = ReadUInt64(payload, 0), fileObject = ReadUInt64(payload, 8);
                uint options = ReadUInt32(payload, 20);
                string name = Text(payload, nameOffset);
                enumerated.Remove(fileObject);
                bool kept = Kept(name);
                if (kept) nameByObject[fileObject] = name;
                else nameByObject.Remove(fileObject);
                uint disposition = options >> 24;
                if (disposition == FileOpen)
                {
                    if (kept)
                        pending[irp] = new Pending { Pid = pid, Operation = OpenOperation(options), Path = name };
                }
                else if (disposition <= FileOverwriteIf && (kept || KeptWrite(name)))
                {
                    string readOperation = disposition == FileOpenIf && kept ? OpenOperation(options) : null;
                    pending[irp] = new Pending { Pid = pid, Operation = "write", ReadOperation = readOperation, Path = name, IsWrite = true };
                }
                break;
            }
            case 20:
            {
                if (payload.Length < 40) break;
                ulong irp = ReadUInt64(payload, 0), fileObject = ReadUInt64(payload, 8);
                string directory;
                if (!nameByObject.TryGetValue(fileObject, out directory)) nameByKey.TryGetValue(ReadUInt64(payload, 16), out directory);
                // Only the first query of an open starts a listing; the queries that continue it add no line.
                if (directory == null || !enumerated.Add(fileObject)) break;
                pending[irp] = new Pending { Pid = pid, Operation = "enum", Path = directory.TrimEnd('\\') + "\\" + Text(payload, 40), EachTime = true };
                break;
            }
            case 24:
            {
                if (payload.Length < 20) break;
                Pending operation;
                if (!pending.Remove(ReadUInt64(payload, 0), out operation)) break;
                ulong extraInfo = ReadUInt64(payload, 8);
                uint status = ReadUInt32(payload, 16);
                if (status < 0xC0000000)
                {
                    Write(operation.Pid, operation.Operation, operation.Path, operation.EachTime);
                    if (operation.ReadOperation != null && extraInfo == FileOpened)
                        Write(operation.Pid, operation.ReadOperation, operation.Path, false);
                }
                else if (!operation.IsWrite && NotFound(status))
                {
                    Write(operation.Pid, operation.Operation == "enum" ? "enum-absent" : "absent", operation.Path, operation.EachTime);
                }
                break;
            }
        }
    }

    void Started(byte version, ReadOnlySpan<byte> payload)
    {
        // ProcessStart: ProcessID, ProcessSequenceNumber, CreateTime, ParentProcessID, ParentProcessSequenceNumber,
        // SessionID, Flags, ProcessTokenElevationType, ProcessTokenIsElevated, MandatoryLabel (a SID), ImageName, ...
        uint pid = ReadUInt32(payload, 0), parent = ReadUInt32(payload, 20);
        string image = "";
        if (payload[48] == 1 && payload[49] <= 15) image = Text(payload, 56 + 4 * payload[49]);
        Flush(pid);
        startedWithoutCommandLine.Remove(pid);
        parentOf[pid] = parent;
        if (version >= 1) sequenceOf[pid] = ReadUInt64(payload, 4);
        else sequenceOf.Remove(pid);
        written.Remove(pid);
        StartInfo pendingCommandLine;
        if (commandLineOf.TryGetValue(pid, out pendingCommandLine))
        {
            commandLineOf.Remove(pid);
            if (pendingCommandLine.Parent == parent)
            {
                WriteStart(pid, image, pendingCommandLine.Value);
                return;
            }
        }
        imageOf[pid] = new StartInfo { Parent = parent, Value = image };
    }

    void Stopped(byte version, ReadOnlySpan<byte> payload)
    {
        uint pid = ReadUInt32(payload, 0);
        if (version >= 1 && payload.Length >= 12)
        {
            ulong sequence = ReadUInt64(payload, 4);
            ulong active;
            if (sequenceOf.TryGetValue(pid, out active) && active != sequence) return;
        }
        Flush(pid);
        if (parentOf.ContainsKey(pid) || written.ContainsKey(pid)) Write(pid, "stop", "", true);
        parentOf.Remove(pid);
        sequenceOf.Remove(pid);
        written.Remove(pid);
        commandLineOf.Remove(pid);
        startedWithoutCommandLine.Remove(pid);
    }

    public void ProcessCommandLine(uint pid, uint parent, string commandLine)
    {
        if (commandLine == null) commandLine = "";
        StartInfo pendingImage;
        if (imageOf.TryGetValue(pid, out pendingImage))
        {
            if (pendingImage.Parent == parent)
            {
                imageOf.Remove(pid);
                WriteStart(pid, pendingImage.Value, commandLine);
            }
            return;
        }
        uint flushedParent;
        if (startedWithoutCommandLine.TryGetValue(pid, out flushedParent) && flushedParent == parent)
        {
            startedWithoutCommandLine.Remove(pid);
            return;
        }
        commandLineOf[pid] = new StartInfo { Parent = parent, Value = commandLine };
    }

    public void FlushAll()
    {
        foreach (uint pid in new List<uint>(imageOf.Keys)) Flush(pid);
    }

    // Write a start that still waits for its command line, without its fifth column, before any other line of the
    // process or its children; check fails on such a start of a git process.
    void Flush(uint pid)
    {
        StartInfo info;
        if (!imageOf.Remove(pid, out info)) return;
        startedWithoutCommandLine[pid] = info.Parent;
        WriteStart(pid, info.Value, null);
    }

    void WriteStart(uint pid, string image, string commandLine)
    {
        uint parent;
        if (parentOf.TryGetValue(pid, out parent)) Flush(parent);
        Write(pid, "start", commandLine == null ? image : image + "\t" + Escape(commandLine));
    }

    static string Escape(string text) => text.Replace("\\", "\\\\").Replace("\t", "\\t").Replace("\n", "\\n").Replace("\r", "\\r");

    void Write(uint pid, string operation, string path, bool eachTime = false)
    {
        if (operation != "start" && operation != "stop") Flush(pid);
        HashSet<string> seen;
        if (!written.TryGetValue(pid, out seen)) written[pid] = seen = new HashSet<string>(StringComparer.Ordinal);
        if (!seen.Add(operation + "\t" + path) && !eachTime) return;
        uint parent;
        Lines++;
        writer.Write(pid);
        writer.Write('\t');
        if (parentOf.TryGetValue(pid, out parent)) writer.Write(parent);
        writer.Write('\t');
        writer.Write(operation);
        writer.Write('\t');
        writer.WriteLine(path);
    }
}
