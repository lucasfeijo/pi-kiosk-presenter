#!/usr/bin/env bash

# Shared by the fresh installer and updates on an existing Pi.
install_airplay_dependencies() {
    local packages=(
        uxplay avahi-daemon
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good
        gstreamer1.0-plugins-bad gstreamer1.0-libav
        gstreamer1.0-x gstreamer1.0-alsa
    )
    local missing=() package
    for package in "${packages[@]}"; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
            missing+=("$package")
        fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "Installing AirPlay packages: ${missing[*]}"
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    fi
    sudo systemctl enable --now avahi-daemon
}
