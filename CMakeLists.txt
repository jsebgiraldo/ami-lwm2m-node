cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

project(ami-lwm2m-client
    VERSION 0.4.0
    DESCRIPTION "AMI Node: LwM2M client over Thread (ESP32-C6)"
    LANGUAGES C
)

# Source files
target_sources(app PRIVATE
    src/main.c
    src/lwm2m_obj_power_meter.c
    src/firmware_update.c
)

# Include path for custom LwM2M object internal headers
target_include_directories(app PRIVATE
    ${ZEPHYR_BASE}/subsys/net/lib/lwm2m
)
