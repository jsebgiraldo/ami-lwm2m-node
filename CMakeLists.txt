cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

project(ami-lwm2m-client
    VERSION 0.6.43
    DESCRIPTION "AMI Node: LwM2M client over Thread (ESP32-C6)"
    LANGUAGES C
)

# Source files
target_sources(app PRIVATE
    src/main.c
    src/lwm2m_discover.c
    src/lwm2m_watchdog.c
    src/hw_watchdog.c
    src/rgb_led.c
    src/lwm2m_obj_power_meter.c
    src/firmware_update.c
    src/thread_conn_monitor.c
    src/lwm2m_obj_thread_net.c
    src/lwm2m_obj_thread_neighbor.c
    src/lwm2m_obj_thread_commission.c
    src/lwm2m_obj_thread_cli.c
    src/rs485_uart.c
    src/dlms_hdlc.c
    src/dlms_cosem.c
    src/dlms_meter.c
    src/post_mortem.c
    src/lwm2m_obj_light_control.c
    src/lwm2m_obj_thread_role.c
    src/coap_keepalive.c
    src/coap_diag.c
)

# Include path for custom LwM2M object internal headers
target_include_directories(app PRIVATE
    ${ZEPHYR_BASE}/subsys/net/lib/lwm2m
)

# CoAP diag server (src/coap_diag.c): COAP_SERVICE_DEFINE(diag_service, ...)
# places its resources into a per-service iterable section
# "coap_resource_diag_service". Register that section on BOTH linker paths so
# the _coap_resource_diag_service_list_start/end symbols resolve. See
# sections-ram.ld and samples/net/sockets/coap_server for the upstream pattern.
zephyr_linker_sources(DATA_SECTIONS sections-ram.ld)
zephyr_iterable_section(
    NAME coap_resource_diag_service
    GROUP DATA_REGION ${XIP_ALIGN_WITH_INPUT})
