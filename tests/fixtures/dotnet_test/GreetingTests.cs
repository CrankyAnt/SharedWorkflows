using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace GreetingFixture;

[TestClass]
public class GreetingTests
{
    [TestMethod]
    public void GreetsByName()
    {
        Assert.AreEqual("Hello, CI!", Greeting.For("CI"));
    }
}
