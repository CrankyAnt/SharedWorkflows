using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace GreetingProof;

[TestClass]
public class GreetingTests
{
    [TestMethod]
    public void GreetsByName()
    {
        Assert.AreEqual("Hello, CI", Greeting.For("CI"));
    }
}
