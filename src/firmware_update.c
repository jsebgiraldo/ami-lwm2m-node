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
#include <zephyr/sys/reboot.h>
#if defined(CONFIG_BOOTLOADER_MCUBOOT)
#include <zephyr/dfu/flash_img.h>
#include <zephyr/dfu/mcuboot.h>
#endif

LOG_MODULE_REGISTER(fw_update, LOG_LEVEL_INF);

/* v0.7.18: reboot-cause tag (Object 33000 RID 37), defined in main.c. */
extern void ami_reboot_set_tag(uint32_t code);
#define AMI_RBOOT_OTA_APPLY   16   /* cold reboot to apply a staged image */

/* Scratch buffer for incoming firmware blocks */
static uint8_t firmware_buf[256];

/* Track download progress */
static size_t total_bytes_received;

/* Supported PULL protocol: 0 = CoAP */
static uint8_t supported_protocol[1] = { 0 };

#if defined(CONFIG_BOOTLOADER_MCUBOOT)
/* v0.6.27 real OTA: stream incoming blocks into slot1 (image-1) via the
 * Zephyr flash_img / stream_flash helpers. On Execute (RID 2) we mark slot1
 * as the pending upgrade and cold-reboot; MCUboot swaps it into slot0 and
 * runs it. If the new image never confirms (see boot_write_img_confirmed in
 * main once registered), MCUboot reverts to the previous image on the next
 * reset — a free rollback safety net. */
static struct flash_img_context fw_flash_ctx;
static bool fw_flash_active;
#endif

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
#if defined(CONFIG_BOOTLOADER_MCUBOOT)
		int rc = flash_img_init(&fw_flash_ctx);
		if (rc != 0) {
			LOG_ERR("FW: flash_img_init failed: %d", rc);
			fw_flash_active = false;
			return rc;
		}
		fw_flash_active = true;
		LOG_INF("FW: slot1 stream init OK — writing to image-1");
#endif
	}

	total_bytes_received += data_len;

#if defined(CONFIG_BOOTLOADER_MCUBOOT)
	if (fw_flash_active) {
		/* flash_img_buffered_write buffers internally and erases slot1
		 * progressively (CONFIG_IMG_ERASE_PROGRESSIVELY). Flush on the
		 * last block. */
		int rc = flash_img_buffered_write(&fw_flash_ctx, data, data_len,
						  last_block);
		if (rc != 0) {
			LOG_ERR("FW: flash write failed at offset=%zu: %d",
				offset, rc);
			fw_flash_active = false;
			return rc;
		}
	}
#endif

	LOG_INF("FW: Block offset=%zu len=%u total_rx=%zu%s",
		offset, data_len, total_bytes_received,
		last_block ? " [LAST]" : "");

	if (last_block) {
#if defined(CONFIG_BOOTLOADER_MCUBOOT)
		size_t written = flash_img_bytes_written(&fw_flash_ctx);
		LOG_INF("FW: download complete — %zu bytes in slot1", written);
#endif
	}

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

#if defined(CONFIG_BOOTLOADER_MCUBOOT)
	/* Mark slot1 as the image to boot next (TEST mode → revertible).
	 * MCUboot will swap slot1 into slot0 on the next boot and run it.
	 * If the new firmware does NOT call boot_write_img_confirmed()
	 * (we do that from main() after the first successful REGISTER),
	 * MCUboot reverts to the prior image on the following reset. That
	 * gives us automatic rollback if an OTA image can't get online. */
	int rc = boot_request_upgrade(BOOT_UPGRADE_TEST);
	if (rc != 0) {
		LOG_ERR("FW: boot_request_upgrade failed: %d", rc);
		lwm2m_set_u8(&LWM2M_OBJ(5, 0, 5), RESULT_UPDATE_FAILED);
		return rc;
	}
	lwm2m_set_u8(&LWM2M_OBJ(5, 0, 5), RESULT_SUCCESS);
	LOG_WRN("FW: slot1 marked for upgrade — cold reboot in 2s to apply");
	/* Give the LwM2M stack a moment to ACK the Execute before we drop. */
	k_sleep(K_SECONDS(2));
	/* v0.7.18: tag the OTA reboot (RID 37) so the first boot on the new image
	 * is not misread as a power loss — otherwise every upgrade looks like a
	 * field fault in the reboot-cause census. */
	ami_reboot_set_tag(AMI_RBOOT_OTA_APPLY);
	sys_reboot(SYS_REBOOT_COLD);
	/* unreachable */
	return 0;
#else
	/* No MCUboot in this build — simulate success (legacy/monolithic). */
	lwm2m_set_u8(&LWM2M_OBJ(5, 0, 3), STATE_IDLE);
	lwm2m_set_u8(&LWM2M_OBJ(5, 0, 5), RESULT_SUCCESS);
	LOG_INF("FW: Update simulated OK (no MCUboot — not applied)");
	return 0;
#endif
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
