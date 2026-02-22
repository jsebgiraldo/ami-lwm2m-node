/*
 * Firmware Update (Object 5) Callbacks
 *
 * Implements LwM2M FOTA support for AMI node.
 * Handles firmware block reception (PUSH and PULL modes),
 * state machine transitions, and update execution.
 *
 * Without MCUboot this is a simulated update — blocks are
 * received and logged but not written to flash. The state
 * machine still transitions correctly so the full OTA flow
 * can be validated end-to-end.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>

LOG_MODULE_REGISTER(fw_update, LOG_LEVEL_INF);

/* Scratch buffer for incoming firmware blocks */
static uint8_t firmware_buf[256];

/* Track download progress */
static size_t total_bytes_received;

/* Supported PULL protocol: 0 = CoAP */
static uint8_t supported_protocol[1] = { 0 };

/*
 * Pre-write callback — provides the engine with a buffer
 * to write incoming firmware data blocks into.
 */
static void *firmware_get_buf(uint16_t obj_inst_id, uint16_t res_id,
			      uint16_t res_inst_id, size_t *data_len)
{
	*data_len = sizeof(firmware_buf);
	return firmware_buf;
}

/*
 * Block received callback — called for each block of firmware
 * data, whether PUSH (written to RID 0) or PULL (fetched from URI).
 */
static int firmware_block_received_cb(uint16_t obj_inst_id, uint16_t res_id,
				      uint16_t res_inst_id, uint8_t *data,
				      uint16_t data_len, bool last_block,
				      size_t total_size, size_t offset)
{
	if (offset == 0) {
		total_bytes_received = 0;
		LOG_INF("FW: Download started (total_size=%zu)", total_size);
	}

	total_bytes_received += data_len;

	LOG_INF("FW: Block offset=%zu len=%u total_rx=%zu%s",
		offset, data_len, total_bytes_received,
		last_block ? " [LAST]" : "");

	/*
	 * TODO: With MCUboot enabled, write blocks to flash here:
	 *   flash_img_buffered_write(&flash_ctx, data, data_len, last_block);
	 */

	return 0;
}

/*
 * Update execute callback — called when server triggers RID 2 (Update).
 * The firmware has already been fully downloaded at this point.
 */
static int firmware_update_cb(uint16_t obj_inst_id,
			      uint8_t *args, uint16_t args_len)
{
	LOG_INF("FW: Update requested! Total bytes received: %zu",
		total_bytes_received);

	/*
	 * TODO: With MCUboot enabled, apply the update:
	 *   boot_request_upgrade(BOOT_UPGRADE_TEST);
	 *   sys_reboot(SYS_REBOOT_COLD);
	 *
	 * For now (no MCUboot), simulate success:
	 */
	lwm2m_set_u8(&LWM2M_OBJ(5, 0, 3), STATE_IDLE);
	lwm2m_set_u8(&LWM2M_OBJ(5, 0, 5), RESULT_SUCCESS);

	LOG_INF("FW: Update simulated OK (no MCUboot — not applied)");
	return 0;
}

/*
 * Cancel callback — called when download is cancelled.
 */
static int firmware_cancel_cb(const uint16_t obj_inst_id)
{
	LOG_INF("FW: Update cancelled");
	total_bytes_received = 0;
	return 0;
}

/*
 * Initialize firmware update callbacks.
 * Call this from lwm2m_setup() before starting the RD client.
 */
void init_firmware_update(void)
{
	/* Provide scratch buffer for incoming firmware blocks */
	lwm2m_register_pre_write_callback(&LWM2M_OBJ(5, 0, 0),
					  firmware_get_buf);

	/* Register block write callback */
	lwm2m_firmware_set_write_cb(firmware_block_received_cb);

	/* Register cancel callback */
	lwm2m_firmware_set_cancel_cb(firmware_cancel_cb);

	/* Register update (execute) callback */
	lwm2m_firmware_set_update_cb(firmware_update_cb);

	/* Declare supported PULL protocol (CoAP = 0) */
	lwm2m_create_res_inst(&LWM2M_OBJ(5, 0, 8, 0));
	lwm2m_set_res_buf(&LWM2M_OBJ(5, 0, 8, 0),
			  &supported_protocol[0],
			  sizeof(supported_protocol[0]),
			  sizeof(supported_protocol[0]), 0);

	LOG_INF("FW: Firmware update callbacks registered (PUSH+PULL)");
}
