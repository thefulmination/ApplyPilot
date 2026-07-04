"""Host-identity guard: a box must REFUSE to run apply workers that belong to a
DIFFERENT machine. Prevents the live 7/04 footgun where a `-Label m2` agent/worker
started on the HOME box materialized TARPON's (m2) 4 workers on the home box -- the
home box's own desired count is 0 and it should never physically host another
machine's fleet.

Each box declares its own fleet identity in APPLYPILOT_FLEET_LABEL (e.g. 'home',
'm2', 'm4'); a worker's --machine-owner names WHICH machine's slots it fills. When a
box is labeled and the two disagree, the worker refuses to start."""
import pytest

from applypilot.fleet.apply_worker_main import enforce_host_identity


def test_refuses_foreign_machine_owner_on_labeled_box():
    with pytest.raises(SystemExit) as ei:
        enforce_host_identity("m2", env={"APPLYPILOT_FLEET_LABEL": "home"})
    msg = str(ei.value)
    assert "home" in msg and "m2" in msg  # error names both the box and the foreign owner


def test_allows_machine_owner_matching_box_label():
    # Must NOT raise: a home worker on the home box is exactly right.
    enforce_host_identity("home", env={"APPLYPILOT_FLEET_LABEL": "home"})


def test_match_is_case_and_whitespace_insensitive():
    # Env/label drift (casing, stray spaces) must not cause a false refusal.
    enforce_host_identity("  Home ", env={"APPLYPILOT_FLEET_LABEL": "home"})


def test_unlabeled_box_is_permissive_for_backcompat():
    # Box identity unknown -> cannot guard -> allow, so boxes that haven't set
    # APPLYPILOT_FLEET_LABEL yet (m2/m4) keep working until they're labeled.
    enforce_host_identity("m2", env={})
    enforce_host_identity("m2", env={"APPLYPILOT_FLEET_LABEL": "   "})


def test_blank_machine_owner_is_allowed():
    # No owner to compare against -> nothing to refuse.
    enforce_host_identity(None, env={"APPLYPILOT_FLEET_LABEL": "home"})
    enforce_host_identity("", env={"APPLYPILOT_FLEET_LABEL": "home"})
