from app.keepalive import MIN_INTERVAL_S, keepalive_interval, keepalive_url


def test_off_without_a_public_url():
    assert keepalive_url({}) is None


def test_uses_render_url_and_appends_health_path():
    assert keepalive_url({"RENDER_EXTERNAL_URL": "https://gamas-api.onrender.com/"}) == (
        "https://gamas-api.onrender.com/api/health"
    )


def test_explicit_url_wins_and_can_be_disabled():
    env = {"SPHEREX_KEEPALIVE_URL": "https://x.example/api/health", "RENDER_EXTERNAL_URL": "https://y.example"}
    assert keepalive_url(env) == "https://x.example/api/health"
    assert keepalive_url({**env, "SPHEREX_KEEPALIVE": "0"}) is None


def test_interval_has_a_floor_and_tolerates_garbage():
    assert keepalive_interval({"SPHEREX_KEEPALIVE_S": "5"}) == MIN_INTERVAL_S
    assert keepalive_interval({"SPHEREX_KEEPALIVE_S": "abc"}) == 600.0
