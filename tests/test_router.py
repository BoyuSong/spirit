from companion.models.router import ModelRouter


def test_router():
    router = ModelRouter()
    assert router.select_role("chat") == "primary"
    assert router.select_role("coding") == "coding"
    assert router.select_role("unknown") == "primary"
