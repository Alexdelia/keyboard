import pcbnew


def test_pcbnew_importable() -> None:
	assert pcbnew.Version().split(".")[0].isdigit()
