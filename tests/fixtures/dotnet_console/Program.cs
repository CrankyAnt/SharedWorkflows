// The console test program of the fixture: it has no test framework, and its exit code is the result of its checks.
namespace GreetingConsoleFixture;

public static class Greeting
{
    public static string For(string name) => $"Hello, {name}";
}

public static class Program
{
    public static int Main()
    {
        var failed = 0;
        Check("greets by name", "Hello, CI", Greeting.For("CI"), ref failed);
        Console.WriteLine(failed == 0 ? "All checks passed." : $"{failed} check(s) failed.");
        return failed == 0 ? 0 : 1;
    }

    private static void Check(string name, string expected, string actual, ref int failed)
    {
        if (expected == actual)
        {
            Console.WriteLine($"passed: {name}");
            return;
        }
        Console.WriteLine($"failed: {name}: expected \"{expected}\", found \"{actual}\"");
        failed++;
    }
}
