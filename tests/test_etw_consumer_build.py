"""The committed build of the ETW consumer belongs to its committed source."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import temporary_support

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPOSITORY_ROOT / "actions" / "recording" / "etw_consumer_build.py"


@unittest.skipUnless(sys.platform == "win32", "the consumer's build is byte-reproducible on Windows only")
class CommittedBuildTest(unittest.TestCase):
    def test_a_rebuild_of_the_source_equals_the_committed_build(self) -> None:
        if shutil.which("dotnet") is None:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.fail("dotnet is not on PATH: the job runs this entry file without its dotnet setup")
            self.skipTest("the pinned .NET SDK is not on PATH")
        result = subprocess.run([sys.executable, str(BUILD_SCRIPT), "check"], cwd=REPOSITORY_ROOT,
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class PublishCommandTest(unittest.TestCase):
    def test_the_build_runs_without_build_servers_and_node_reuse(self) -> None:
        spec = importlib.util.spec_from_file_location("etw_consumer_build", BUILD_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        work = temporary_support.make_directory(self.addCleanup)
        with mock.patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            module.publish(work / "build", work / "artifacts")
        command = run.call_args.args[0]
        self.assertEqual(["dotnet", "publish"], command[:2])
        for flag in ("--disable-build-servers", "-nodeReuse:false", "-p:UseSharedCompilation=false"):
            self.assertIn(flag, command)


CONSUMER_DLL = REPOSITORY_ROOT / "actions" / "recording" / "etw-consumer" / "build" / "EtwConsumer.dll"

EVENT_PROCESSOR_HARNESS = r"""
using System;
using System.Buffers.Binary;
using System.IO;
using System.Text;

static class Harness
{
    static readonly Guid KernelFile = Program.KernelFile;
    static readonly Guid KernelProcess = Program.KernelProcess;

    static byte[] NameCreate(ulong fileKey, string path)
    {
        byte[] name = Encoding.Unicode.GetBytes(path + "\0");
        byte[] buf = new byte[8 + name.Length];
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(0, 8), fileKey);
        name.CopyTo(buf, 8);
        return buf;
    }

    static byte[] CreateEvent(ulong irp, ulong fileObject, uint options, uint share, string path, byte version = 1)
    {
        int nameOffset = version >= 1 ? 32 : 28;
        byte[] name = Encoding.Unicode.GetBytes(path + "\0");
        byte[] buf = new byte[nameOffset + name.Length];
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(0, 8), irp);
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(8, 8), fileObject);
        BinaryPrimitives.WriteUInt32LittleEndian(buf.AsSpan(20, 4), options);
        if (version >= 1) BinaryPrimitives.WriteUInt32LittleEndian(buf.AsSpan(28, 4), share);
        name.CopyTo(buf, nameOffset);
        return buf;
    }

    static byte[] DirEnumEvent(ulong irp, ulong fileObject, ulong fileKey, string pattern)
    {
        byte[] name = Encoding.Unicode.GetBytes(pattern + "\0");
        byte[] buf = new byte[40 + name.Length];
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(0, 8), irp);
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(8, 8), fileObject);
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(16, 8), fileKey);
        name.CopyTo(buf, 40);
        return buf;
    }

    static byte[] OpEndEvent(ulong irp, ulong extraInfo, uint status)
    {
        byte[] buf = new byte[20];
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(0, 8), irp);
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(8, 8), extraInfo);
        BinaryPrimitives.WriteUInt32LittleEndian(buf.AsSpan(16, 4), status);
        return buf;
    }

    static byte[] ProcessStartEvent(uint pid, ulong sequence, uint parent, string image)
    {
        byte[] name = Encoding.Unicode.GetBytes(image + "\0");
        byte[] buf = new byte[60 + name.Length];
        BinaryPrimitives.WriteUInt32LittleEndian(buf.AsSpan(0, 4), pid);
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(4, 8), sequence);
        BinaryPrimitives.WriteUInt32LittleEndian(buf.AsSpan(20, 4), parent);
        buf[48] = 1;
        buf[49] = 1;
        name.CopyTo(buf, 60);
        return buf;
    }

    static byte[] ProcessStopEvent(uint pid, ulong sequence)
    {
        byte[] buf = new byte[12];
        BinaryPrimitives.WriteUInt32LittleEndian(buf.AsSpan(0, 4), pid);
        BinaryPrimitives.WriteUInt64LittleEndian(buf.AsSpan(4, 8), sequence);
        return buf;
    }

    static void Assert(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }

    static string Run(Action<EventProcessor> drive)
    {
        var output = new StringWriter { NewLine = "\n" };
        var processor = new EventProcessor(output, new[] { @"\Device\HarddiskVolume4\repo" }, new[] { @"\Device\HarddiskVolume3", @"\Device\HarddiskVolume4" });
        drive(processor);
        processor.FlushAll();
        return output.ToString();
    }

    static int Main()
    {
        // 1. Truncated payloads of every event type are rejected without corrupting subsequent valid events.
        string truncated = Run(p =>
        {
            p.ProcessEvent(KernelProcess, 1, 1, 100, new byte[0]);
            p.ProcessEvent(KernelProcess, 1, 1, 100, new byte[55]);
            p.ProcessEvent(KernelProcess, 2, 1, 100, new byte[0]);
            p.ProcessEvent(KernelProcess, 2, 1, 100, new byte[3]);
            p.ProcessEvent(KernelFile, 10, 0, 100, new byte[0]);
            p.ProcessEvent(KernelFile, 10, 0, 100, new byte[7]);
            p.ProcessEvent(KernelFile, 12, 0, 100, new byte[27]);
            p.ProcessEvent(KernelFile, 12, 1, 100, new byte[31]);
            p.ProcessEvent(KernelFile, 20, 0, 100, new byte[39]);
            p.ProcessEvent(KernelFile, 24, 0, 100, new byte[19]);
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(1, 10, 1u << 24, 1, @"\Device\HarddiskVolume4\repo\ok.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(1, 1, 0));
        });
        Assert(truncated == "100\t\topen\t\\Device\\HarddiskVolume4\\repo\\ok.txt\n", "truncated: " + truncated);

        // 2. STATUS_NOT_A_DIRECTORY (0xC0000103) becomes absent / enum-absent.
        string notDir = Run(p =>
        {
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(1, 10, 1u << 24, 1, @"\Device\HarddiskVolume4\repo\file.txt\child"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(1, 0, 0xC0000103));
            p.ProcessEvent(KernelFile, 10, 0, 100, NameCreate(20, @"\Device\HarddiskVolume4\repo\file.txt"));
            p.ProcessEvent(KernelFile, 20, 0, 100, DirEnumEvent(2, 30, 20, "*"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(2, 0, 0xC0000103));
        });
        Assert(notDir == "100\t\tabsent\t\\Device\\HarddiskVolume4\\repo\\file.txt\\child\n100\t\tenum-absent\t\\Device\\HarddiskVolume4\\repo\\file.txt\\*\n",
            "notDir: " + notDir);

        // 3. Share mode 0 alone stays open; FILE_OPEN_REPARSE_POINT without directory/non-directory is openattr.
        string shareModes = Run(p =>
        {
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(1, 10, 1u << 24, 0, @"\Device\HarddiskVolume4\repo\exclusive.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(1, 1, 0));
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(2, 11, (1u << 24) | 0x200000u, 0, @"\Device\HarddiskVolume4\repo\stat.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(2, 1, 0));
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(3, 12, (1u << 24) | 0x200040u, 0, @"\Device\HarddiskVolume4\repo\nondir.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(3, 1, 0));
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(4, 13, (1u << 24) | 0x1u, 0, @"\Device\HarddiskVolume4\repo\dir"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(4, 1, 0));
        });
        Assert(shareModes ==
            "100\t\topen\t\\Device\\HarddiskVolume4\\repo\\exclusive.txt\n" +
            "100\t\topenattr\t\\Device\\HarddiskVolume4\\repo\\stat.txt\n" +
            "100\t\topen\t\\Device\\HarddiskVolume4\\repo\\nondir.txt\n" +
            "100\t\topendir\t\\Device\\HarddiskVolume4\\repo\\dir\n",
            "shareModes: " + shareModes);

        // 4. PID reuse: ProcessStop clears maps, stale CommandLine with old parent is not paired, stale ProcessStop ignored.
        string pidReuse = Run(p =>
        {
            p.ProcessEvent(KernelProcess, 1, 1, 10, ProcessStartEvent(300, 1, 10, @"C:\first.exe"));
            p.ProcessCommandLine(300, 10, "first.exe arg1");
            p.ProcessEvent(KernelFile, 12, 1, 300, CreateEvent(1, 10, 1u << 24, 1, @"\Device\HarddiskVolume4\repo\same.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 300, OpEndEvent(1, 1, 0));
            p.ProcessEvent(KernelProcess, 2, 1, 300, ProcessStopEvent(300, 1));
            // Stale command line for old parent 10 before new process 300 under parent 20 starts.
            p.ProcessCommandLine(300, 10, "stale-cmd");
            p.ProcessEvent(KernelProcess, 1, 1, 20, ProcessStartEvent(300, 2, 20, @"C:\second.exe"));
            // Out-of-order ProcessStop for sequence 1 must not clear sequence 2.
            p.ProcessEvent(KernelProcess, 2, 1, 300, ProcessStopEvent(300, 1));
            p.ProcessCommandLine(300, 20, "second.exe arg2");
            p.ProcessEvent(KernelFile, 12, 1, 300, CreateEvent(2, 11, 1u << 24, 1, @"\Device\HarddiskVolume4\repo\same.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 300, OpEndEvent(2, 1, 0));
        });
        Assert(pidReuse ==
            "300\t10\tstart\tC:\\first.exe\tfirst.exe arg1\n" +
            "300\t10\topen\t\\Device\\HarddiskVolume4\\repo\\same.txt\n" +
            "300\t10\tstop\t\n" +
            "300\t20\tstart\tC:\\second.exe\tsecond.exe arg2\n" +
            "300\t20\topen\t\\Device\\HarddiskVolume4\\repo\\same.txt\n",
            "pidReuse: " + pidReuse);

        // 5. Write dispositions inside and outside prefixes, FILE_OPEN_IF existing vs created, failed writes, non-DOS devices.
        string writes = Run(p =>
        {
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(1, 10, 5u << 24, 1, @"\Device\HarddiskVolume4\repo\out.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(1, 2, 0));
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(2, 11, 2u << 24, 1, @"\Device\HarddiskVolume3\Users\runner\leaked.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(2, 2, 0));
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(3, 12, 3u << 24, 1, @"\Device\HarddiskVolume4\repo\existing.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(3, 1, 0));  // FILE_OPENED -> write + open
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(4, 13, 3u << 24, 1, @"\Device\HarddiskVolume4\repo\created.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(4, 2, 0));  // FILE_CREATED -> write only
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(5, 14, 2u << 24, 1, @"\Device\HarddiskVolume4\repo\missing\fail.txt"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(5, 0, 0xC000003A));  // failed write -> nothing
            p.ProcessEvent(KernelFile, 12, 1, 100, CreateEvent(6, 15, 3u << 24, 1, @"\Device\NamedPipe\pipe"));
            p.ProcessEvent(KernelFile, 24, 0, 100, OpEndEvent(6, 1, 0));  // non-DOS device -> nothing
        });
        Assert(writes ==
            "100\t\twrite\t\\Device\\HarddiskVolume4\\repo\\out.txt\n" +
            "100\t\twrite\t\\Device\\HarddiskVolume3\\Users\\runner\\leaked.txt\n" +
            "100\t\twrite\t\\Device\\HarddiskVolume4\\repo\\existing.txt\n" +
            "100\t\topen\t\\Device\\HarddiskVolume4\\repo\\existing.txt\n" +
            "100\t\twrite\t\\Device\\HarddiskVolume4\\repo\\created.txt\n",
            "writes: " + writes);

        return 0;
    }
}
"""

HARNESS_PROJECT = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <UseAppHost>false</UseAppHost>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include="{dll}" />
  </ItemGroup>
</Project>
"""


@unittest.skipUnless(sys.platform == "win32", "the ETW consumer runs on Windows only")
class ConsumerEventProcessorTests(unittest.TestCase):
    def test_event_processor_checks_lengths_not_a_directory_pid_reuse_share_modes_and_writes(self) -> None:
        if shutil.which("dotnet") is None:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.fail("dotnet is not on PATH: the job runs this entry file without its dotnet setup")
            self.skipTest("the pinned .NET SDK is not on PATH")
        work = temporary_support.make_directory(self.addCleanup, prefix="etw-processor-")
        (work / "Harness.csproj").write_text(HARNESS_PROJECT.format(dll=CONSUMER_DLL), encoding="utf-8")
        (work / "Program.cs").write_text(EVENT_PROCESSOR_HARNESS, encoding="utf-8")
        build = subprocess.run(["dotnet", "build", str(work / "Harness.csproj"), "--nologo", "--disable-build-servers",
                                "-nodeReuse:false", "-p:UseSharedCompilation=false", "--output", str(work / "out")],
                               cwd=work, capture_output=True, text=True, check=False)
        self.assertEqual(0, build.returncode, build.stdout + build.stderr)
        ran = subprocess.run(["dotnet", str(work / "out" / "Harness.dll")], cwd=work, capture_output=True, text=True, check=False)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)


if __name__ == "__main__":
    unittest.main()
