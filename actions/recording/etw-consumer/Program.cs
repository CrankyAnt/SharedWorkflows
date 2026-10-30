// Read a live Kernel-File ETW session and write each event's process id, parent process id, operation and path to a
// neutral event file, one tab-separated line per event in stream order. The recording step's check classifies the file.
//
//   dotnet EtwConsumer.dll <session> <event file> <ready file> <stop file> <path prefix> ...
//
// Operations: start (a process start; the path is its image, and a fifth column holds its command line with
// backslashes, tabs and line breaks escaped; a start written before its command line came has no fifth column), open
// and opendir (a successful open of an existing file or directory),
// absent (an open of a missing path), enum and enum-absent (a directory enumeration; the path is the directory, a
// backslash and the queried pattern). Opens that may create or overwrite a file are writes and are not written. File
// events count only under a path prefix, an NT device path; a process's repeated event is written once, except an
// enumeration: the first enumeration of each open of a directory is written every time, so each listing is one line.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Parsers.Kernel;
using Microsoft.Diagnostics.Tracing.Session;

static unsafe class Program
{
    static readonly Guid KernelFile = new Guid("edd08927-9cc4-4e65-b970-c2560fb5c289");
    static readonly Guid KernelProcess = new Guid("22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716");
    const ulong FileKeywords = 0xF0;  // file names, FILEIO (directory enumerations), operation ends, creates
    const ulong ProcessKeywords = 0x10;
    const uint FileOpen = 1;  // the create disposition of an open that neither creates nor overwrites
    const uint DirectoryFile = 0x1;

    struct Pending
    {
        public uint Pid;
        public string Operation;
        public string Path;
        public bool EachTime;  // a listing, written however often the process lists the directory
    }

    static string[] prefixes;
    static StreamWriter writer;
    static readonly Dictionary<ulong, string> nameByObject = new Dictionary<ulong, string>();
    static readonly Dictionary<ulong, string> nameByKey = new Dictionary<ulong, string>();
    static readonly Dictionary<ulong, Pending> pending = new Dictionary<ulong, Pending>();
    static readonly Dictionary<uint, uint> parentOf = new Dictionary<uint, uint>();
    static readonly Dictionary<uint, HashSet<string>> written = new Dictionary<uint, HashSet<string>>();
    static readonly HashSet<ulong> enumerated = new HashSet<ulong>();  // the file objects enumerated since their open
    // A process start comes as two events, the Kernel-Process start with the image and the kernel process start with the
    // command line, in either order; the first one waits here for the second.
    static readonly Dictionary<uint, string> imageOf = new Dictionary<uint, string>();
    static readonly Dictionary<uint, string> commandLineOf = new Dictionary<uint, string>();
    static readonly HashSet<uint> startedWithoutCommandLine = new HashSet<uint>();  // a start written without its command line
    static long events, lines;

    static int Main(string[] args)
    {
        if (args.Length < 5)
        {
            Console.Error.WriteLine("usage: EtwConsumer <session> <event file> <ready file> <stop file> <path prefix> ...");
            return 2;
        }
        prefixes = new string[args.Length - 4];
        for (int index = 4; index < args.Length; index++) prefixes[index - 4] = args[index].TrimEnd('\\');
        writer = new StreamWriter(args[1], false, new UTF8Encoding(false), 1 << 20) { NewLine = "\n" };
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
            foreach (uint pid in new List<uint>(imageOf.Keys)) Flush(pid);
            writer.Dispose();
        }
        Console.WriteLine("drained_ms=" + drained + " events=" + events + " lines=" + lines + " lost=" + lost);
        return 0;
    }

    static ulong Pointer(byte* data, int offset) => *(ulong*)(data + offset);

    static uint Integer(byte* data, int offset) => *(uint*)(data + offset);

    static string Text(byte* data, int offset, int length)
    {
        if (offset >= length) return "";
        int limit = (length - offset) / 2;
        char* characters = (char*)(data + offset);
        int count = 0;
        while (count < limit && characters[count] != 0) count++;
        return new string(characters, 0, count);
    }

    static bool Kept(string path)
    {
        foreach (string prefix in prefixes)
        {
            if (path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) && (path.Length == prefix.Length || path[prefix.Length] == '\\'))
                return true;
        }
        return false;
    }

    static bool NotFound(uint status) => status == 0xC0000034 || status == 0xC000003A || status == 0xC000000F;

    static void OnEvent(TraceEvent data)
    {
        events++;
        int id = (int)data.ID;
        byte* payload = (byte*)data.DataStart;
        int length = data.EventDataLength;
        if (data.ProviderGuid == KernelProcess)
        {
            if (id == 1 && length >= 56) Started(payload, length);
            return;
        }
        if (data.ProviderGuid != KernelFile) return;
        uint pid = (uint)data.ProcessID;
        switch (id)
        {
            case 10:
            {
                string name = Text(payload, 8, length);
                if (Kept(name)) nameByKey[Pointer(payload, 0)] = name;
                break;
            }
            case 12:
            {
                ulong irp = Pointer(payload, 0), fileObject = Pointer(payload, 8);
                uint options = Integer(payload, 20);
                string name = Text(payload, 32, length);
                enumerated.Remove(fileObject);
                if (!Kept(name))
                {
                    nameByObject.Remove(fileObject);
                    break;
                }
                nameByObject[fileObject] = name;
                if (options >> 24 == FileOpen)
                    pending[irp] = new Pending { Pid = pid, Operation = (options & DirectoryFile) != 0 ? "opendir" : "open", Path = name };
                break;
            }
            case 20:
            {
                ulong irp = Pointer(payload, 0), fileObject = Pointer(payload, 8);
                string directory;
                if (!nameByObject.TryGetValue(fileObject, out directory)) nameByKey.TryGetValue(Pointer(payload, 16), out directory);
                // Only the first query of an open starts a listing; the queries that continue it add no line.
                if (directory == null || !enumerated.Add(fileObject)) break;
                pending[irp] = new Pending { Pid = pid, Operation = "enum", Path = directory.TrimEnd('\\') + "\\" + Text(payload, 40, length), EachTime = true };
                break;
            }
            case 24:
            {
                Pending operation;
                if (!pending.Remove(Pointer(payload, 0), out operation)) break;
                uint status = Integer(payload, 16);
                if (NotFound(status)) Write(operation.Pid, operation.Operation == "enum" ? "enum-absent" : "absent", operation.Path, operation.EachTime);
                else if (status < 0xC0000000) Write(operation.Pid, operation.Operation, operation.Path, operation.EachTime);
                break;
            }
        }
    }

    static void Started(byte* payload, int length)
    {
        // ProcessStart: ProcessID, ProcessSequenceNumber, CreateTime, ParentProcessID, ParentProcessSequenceNumber,
        // SessionID, Flags, ProcessTokenElevationType, ProcessTokenIsElevated, MandatoryLabel (a SID), ImageName, ...
        uint pid = Integer(payload, 0), parent = Integer(payload, 20);
        string image = "";
        if (payload[48] == 1 && payload[49] <= 15) image = Text(payload, 56 + 4 * payload[49], length);
        Flush(pid);
        startedWithoutCommandLine.Remove(pid);
        parentOf[pid] = parent;
        written.Remove(pid);
        string commandLine;
        if (commandLineOf.Remove(pid, out commandLine)) WriteStart(pid, image, commandLine);
        else imageOf[pid] = image;
    }

    static void CommandLine(ProcessTraceData data)
    {
        uint pid = (uint)data.ProcessID;
        string image;
        if (imageOf.Remove(pid, out image)) WriteStart(pid, image, data.CommandLine);
        else if (!startedWithoutCommandLine.Remove(pid)) commandLineOf[pid] = data.CommandLine;
    }

    // Write a start that still waits for its command line, without its fifth column, before any other line of the
    // process or its children; check fails on such a start of a git process.
    static void Flush(uint pid)
    {
        string image;
        if (!imageOf.Remove(pid, out image)) return;
        startedWithoutCommandLine.Add(pid);
        WriteStart(pid, image, null);
    }

    static void WriteStart(uint pid, string image, string commandLine)
    {
        uint parent;
        if (parentOf.TryGetValue(pid, out parent)) Flush(parent);
        Write(pid, "start", commandLine == null ? image : image + "\t" + Escape(commandLine));
    }

    static string Escape(string text) => text.Replace("\\", "\\\\").Replace("\t", "\\t").Replace("\n", "\\n").Replace("\r", "\\r");

    static void Write(uint pid, string operation, string path, bool eachTime = false)
    {
        if (operation != "start") Flush(pid);
        HashSet<string> seen;
        if (!written.TryGetValue(pid, out seen)) written[pid] = seen = new HashSet<string>(StringComparer.Ordinal);
        if (!seen.Add(operation + "\t" + path) && !eachTime) return;
        uint parent;
        lines++;
        writer.Write(pid);
        writer.Write('\t');
        if (parentOf.TryGetValue(pid, out parent)) writer.Write(parent);
        writer.Write('\t');
        writer.Write(operation);
        writer.Write('\t');
        writer.WriteLine(path);
    }
}
