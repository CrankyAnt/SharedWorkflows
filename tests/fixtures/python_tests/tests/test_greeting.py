from greeting import greeting


def test_greeting() -> None:
    assert greeting("CI") == "Hello, CI"
