cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

project(ami-ieee802154-sniffer
    DESCRIPTION "AMI 802.15.4 passive sniffer (ESP32-C6 DevKit) for Wireshark"
    LANGUAGES C
)

target_sources(app PRIVATE src/main.c)
