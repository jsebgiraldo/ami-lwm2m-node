/* SPDX-License-Identifier: Apache-2.0 */

/*
 * Per-service CoAP resource list section for the diag CoAP server
 * (src/coap_diag.c). Zephyr's COAP_SERVICE_DEFINE(diag_service, ...) places its
 * COAP_RESOURCE_DEFINE(...) entries into an iterable section named after the
 * service ("coap_resource_<service>") and references linker-generated
 * _coap_resource_diag_service_list_start/end symbols. Those symbols only exist
 * if the application declares the section here (LD template path) and via
 * zephyr_iterable_section() in CMakeLists.txt (CMake linker-generator path).
 * Matches the upstream sample samples/net/sockets/coap_server/sections-ram.ld.
 */
#include <zephyr/linker/iterable_sections.h>

ITERABLE_SECTION_RAM(coap_resource_diag_service, Z_LINK_ITERABLE_SUBALIGN)
