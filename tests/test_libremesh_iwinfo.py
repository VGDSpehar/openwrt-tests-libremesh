import time
import pytest

def _restart_wifi_and_wait(ssh_command, timeout: int = 5) -> bool:
    """Restart wifi and wait for the hostapd instance to come up.

    Args:
        ssh_command: the ssh_command fixture used to execute commands
        timeout: how long to wait for hostapd before timing out

    Returns:
        True if hostapd signalled readiness in time, False otherwise.
    """
    # Bring Wi‑Fi down and up to apply configuration changes.  The
    # ``wifi`` helper is a wrapper around ``/sbin/wifi`` which
    # triggers netifd reloads.
    ssh_command.run("wifi down")
    time.sleep(2)
    ssh_command.run("wifi up")
    time.sleep(2)

    # Wait for hostapd on the first radio.  The name of the AP varies
    # depending on the target (e.g. phy0‑ap0).  We try to detect the
    # actual name by listing hostapd objects via ubus.  Falling back
    # to hostapd.phy0‑ap0 is sufficient for most images.
    ubus_list_out = ssh_command.run("ubus list | grep hostapd")[0]
    ap_obj = next((line.strip() for line in ubus_list_out if line.startswith("hostapd.")), "hostapd.phy0-ap0")
    # Wait until hostapd reports readiness.  If timed out, the string
    # "timed out" appears in the output.
    result = ssh_command.run(f"ubus -t {timeout} wait_for {ap_obj}")[0]
    return "timed out" not in "\n".join(result)


def _ensure_wifi_enabled(ssh_command) -> None:
    """Ensure that wireless radios are enabled before running tests.

    Many OpenWrt images ship with Wi‑Fi disabled to comply with
    regulatory requirements.  This helper clears the 'disabled'
    option on all radios and commits the configuration.  After
    modifying UCI we restart Wi‑Fi and wait for hostapd to be ready.
    """
    # Try to enable radio0.  radio1 may not exist on all boards.
    ssh_command.run("uci delete wireless.radio0.disabled || true")
    # Some boards expose additional radios (e.g. radio1).  Ignore
    # failures when deleting the option.
    ssh_command.run("uci delete wireless.radio1.disabled || true")
    ssh_command.run("uci commit wireless")
    _restart_wifi_and_wait(ssh_command)


def _get_phy_names(ssh_command) -> list[str]:
    """Return a list of available IEEE 802.11 PHY devices.

    The physical devices are exposed under ``/sys/class/ieee80211``.
    If the directory does not exist or contains no entries, an empty
    list is returned.  Devices will be returned in sorted order.
    """
    stdout, stderr, _ = ssh_command.run("ls -1 /sys/class/ieee80211 || true")
    # Filter out empty lines and sort
    return sorted([line.strip() for line in stdout if line.strip() and not line.startswith("total")])


@pytest.mark.lg_feature("wifi")
def test_iwinfo_basic_info(ssh_command):
    """Verify that ``iwinfo`` reports basic information about the first radio.

    This test is analogous to the ``iwinfo.type`` assertion in the Lua
    test suite.  It ensures that invoking ``iwinfo`` without
    additional parameters shows a device listing and that calling
    ``iwinfo <phy> info`` prints a ``Type`` line containing
    ``nl80211``, which confirms that the driver uses the correct
    backend.
    """
    phy_list = _get_phy_names(ssh_command)
    assert phy_list, "No PHY devices found – make sure your firmware includes wireless drivers"

    # ``iwinfo`` without arguments lists all available devices; ensure
    # our first PHY appears in that listing.
    devices_out = ssh_command.run("iwinfo")[0]
    assert any(phy in " ".join(devices_out) for phy in phy_list), (
        f"Expected at least one of {phy_list} to appear in 'iwinfo' output, got: {devices_out}"
    )

    # Inspect detailed info for the first PHY and check for backend type
    info_out = ssh_command.run(f"iwinfo {phy_list[0]} info")[0]
    combined = "\n".join(info_out)
    assert "Type:" in combined, f"Expected a 'Type:' line in iwinfo output, got: {combined}"
    assert "nl80211" in combined, f"Expected backend 'nl80211' in iwinfo output, got: {combined}"


@pytest.mark.lg_feature("wifi")
def test_iwinfo_scan_has_results(ssh_command):
    """Perform a wireless scan and ensure that at least one network is discovered.

    The original Lua test verified that ``iwinfo.nl80211.scanlist``
    returned a table with one or more entries.  Here we call the
    ``iwinfo <phy> scan`` command and look for lines containing
    ``ESSID:``, which indicate detected networks.  If no networks are
    found, the test will report failure.  Wi‑Fi must be enabled and
    configured for this test to work; the helper below handles this.
    """
    _ensure_wifi_enabled(ssh_command)
    phy_list = _get_phy_names(ssh_command)
    assert phy_list, "No PHY devices available for scanning"

    # Execute a scan on the first PHY.  This may take a few seconds.
    stdout, _, _ = ssh_command.run(f"iwinfo {phy_list[0]} scan")
    # Combine all lines for easier searching
    scan_output = "\n".join(stdout)
    assert "ESSID:" in scan_output, (
        f"No ESSIDs found in scan output.  Raw output:\n{scan_output}"
    )


@pytest.mark.lg_feature("wifi")
def test_iwinfo_unknown_device_returns_error(ssh_command):
    """Ensure that querying an unknown interface with ``iwinfo`` returns an error.

    The Lua test expected an empty table when calling
    ``iwinfo.nl80211.scanlist('foo')``.  On the command line the
    implementation prints a clear error message and exits non‑zero.
    This test asserts that invoking ``iwinfo`` on a non‑existent
    device does not succeed and that the error message references
    ``No such wireless`` or a similar phrasing.
    """
    # Choose a name that is unlikely to exist.  Use exit code and
    # stderr/stdout contents to validate the error.
    stdout, stderr, exit_code = ssh_command.run("iwinfo nonexistent0 info")
    combined = "\n".join(stdout + stderr)
    assert exit_code != 0, "iwinfo unexpectedly succeeded on a non‑existent interface"
    assert (
        "No such wireless" in combined
        or "No such device" in combined
        or "No such wireless backend" in combined
        or "Usage" in combined
    ), (
        f"Unexpected error message when querying non‑existent interface: {combined}"
    )


@pytest.mark.lg_feature("wifi")
def test_iwinfo_channel_configuration(ssh_command):
    """Configure the channel via UCI and verify that ``iwinfo`` reflects the change.

    This test is inspired by the Lua ``test channel`` and ``load_from_uci``
    sections.  It sets the channel on the first PHY to an uncommon
    value (e.g. 4), commits the configuration, reloads Wi‑Fi and then
    asserts that the channel reported by ``iwinfo <phy> info`` matches
    the configured value.  If fewer than one PHY exists, the test is
    skipped.
    """
    phy_list = _get_phy_names(ssh_command)
    if not phy_list:
        pytest.skip("No PHY devices available to test channel configuration")

    # Determine the UCI section corresponding to the first radio.  The
    # common naming convention is radio0, but to be safe we derive
    # it from the PHY number (e.g. phy0 -> radio0).
    # Extract the numeric suffix from the PHY name
    import re
    match = re.match(r"phy(\d+)", phy_list[0])
    radio_idx = match.group(1) if match else "0"
    radio = f"radio{radio_idx}"

    # Pick a channel in the 2.4 GHz band that is usually allowed.  Use
    # channel 4 to avoid interference with common default channels (1,6,11).
    target_channel = "4"

    # Apply the configuration and restart Wi‑Fi
    ssh_command.run(f"uci set wireless.{radio}.disabled=0")
    ssh_command.run(f"uci set wireless.{radio}.channel={target_channel}")
    # Some devices support band definitions; leave as is if undefined.
    ssh_command.run(f"uci commit wireless")
    assert _restart_wifi_and_wait(ssh_command), "Wi‑Fi did not come up after channel configuration"

    # Query iwinfo for the channel.  The channel is reported in the
    # form ``Channel: <n> (<freq> GHz)``.  We look for the numeric
    # prefix.
    info_out = ssh_command.run(f"iwinfo {phy_list[0]} info")[0]
    info_combined = "\n".join(info_out)
    # Use a regex to extract the channel number from the line.  If the
    # regex fails to match we fall back to a simple substring check.
    channel_match = re.search(r"Channel:\s*(\d+)", info_combined)
    actual_channel = channel_match.group(1) if channel_match else None
    assert actual_channel == target_channel, (
        f"Expected channel {target_channel} for {phy_list[0]}, got {actual_channel}.\n"
        f"Full iwinfo output:\n{info_combined}"
    )


@pytest.mark.lg_feature("wifi")
def test_iwinfo_multiple_phys_channel(ssh_command):
    """Verify that devices with multiple PHYs can operate on different channels.

    The original Lua test set distinct channels on ``phy0`` and ``phy1``
    and asserted that the values were preserved.  This test performs
    the same operation if at least two PHYs are present; otherwise it
    is skipped.  After adjusting the channels and restarting Wi‑Fi
    we inspect ``iwinfo`` for each PHY.
    """
    phy_list = _get_phy_names(ssh_command)
    if len(phy_list) < 2:
        pytest.skip("Less than two PHY devices available; skipping multi‑radio channel test")

    import re
    # Determine radio names from PHY indices
    radios = []
    for phy in phy_list[:2]:
        m = re.match(r"phy(\d+)", phy)
        idx = m.group(1) if m else "0"
        radios.append(f"radio{idx}")

    # Configure different channels on the first two radios.  Choose
    # channels unlikely to overlap for 2.4/5 GHz bands.  Channel 1 and 48
    # are commonly supported.
    channel_a = "1"
    channel_b = "48"
    ssh_command.run(f"uci set wireless.{radios[0]}.disabled=0")
    ssh_command.run(f"uci set wireless.{radios[1]}.disabled=0")
    ssh_command.run(f"uci set wireless.{radios[0]}.channel={channel_a}")
    ssh_command.run(f"uci set wireless.{radios[1]}.channel={channel_b}")
    ssh_command.run("uci commit wireless")
    assert _restart_wifi_and_wait(ssh_command), "Wi‑Fi did not come up after multi‑channel configuration"

    # Check iwinfo output for each PHY
    for phy, expected in zip(phy_list[:2], [channel_a, channel_b]):
        info_out = ssh_command.run(f"iwinfo {phy} info")[0]
        info_combined = "\n".join(info_out)
        m = re.search(r"Channel:\s*(\d+)", info_combined)
        actual = m.group(1) if m else None
        assert actual == expected, (
            f"Expected channel {expected} for {phy}, got {actual}.\n"
            f"Full iwinfo output:\n{info_combined}"
        )


@pytest.mark.lg_feature("wifi")
def test_iwinfo_assoclist_no_stations(ssh_command):
    """Ensure that the association list is empty when no stations are connected.

    The Lua suite checked that an empty association list was returned
    when no stations were connected.  Here we query the association
    list for each available PHY and expect either an empty output or
    a message indicating that no stations are associated.  This test
    does not attempt to create a client connection; that is covered by
    the more extensive hwsim tests in ``test_wifi.py``.
    """
    phy_list = _get_phy_names(ssh_command)
    assert phy_list, "No PHY devices available to query association list"

    for phy in phy_list:
        stdout, _, exit_code = ssh_command.run(f"iwinfo {phy} assoclist")
        combined = "\n".join(stdout)
        # Exit code 0 is expected even when no stations are present
        assert exit_code == 0, f"iwinfo returned an unexpected exit code {exit_code} for {phy} assoclist"
        # If no stations are connected, the output typically contains
        # 'No information available' or is empty.  Accept both cases.
        assert (not combined.strip()) or "No information available" in combined or "expected throughput" not in combined, (
            f"Expected no stations in assoclist for {phy}, but got:\n{combined}"
        )
