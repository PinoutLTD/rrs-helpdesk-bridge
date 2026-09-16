import pytest
from pydantic import ValidationError

from rrs_helpdesk_bridge.config import PROFILE_SECRETS, ClientRegistry, EnvSettings


def test_registry_maps_sites_to_partners() -> None:
    registry = ClientRegistry.model_validate(
        {"clients": [{"client_id": "qube-block-a-301", "odoo_partner_id": 7}]}
    )

    assert registry.partner_id("qube-block-a-301") == 7
    assert registry.partner_id("del-mar-24f") is None


def test_client_id_must_be_a_site_slug() -> None:
    with pytest.raises(ValidationError, match="site slug"):
        ClientRegistry.model_validate(
            {"clients": [{"client_id": "Qube-A301", "odoo_partner_id": 7}]}
        )


def test_profile_selects_the_proton_pass_item(tmp_path) -> None:
    settings = EnvSettings(
        _env_file=None,
        profile="prod",
        reports_dir=tmp_path,
        state_db=tmp_path / "state.sqlite3",
        registry_file=tmp_path / "registry.yaml",
    )

    assert settings.secret_location == PROFILE_SECRETS["prod"]
    assert settings.write_enabled is False
