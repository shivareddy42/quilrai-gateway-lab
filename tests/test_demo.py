from quilr_gateway.demo import run


async def test_real_network_demo(capsys):
    await run()
    output = capsys.readouterr().out
    for task in range(1, 5):
        assert f"Task {task}:" in output
    assert "sanitized gateway error" in output
