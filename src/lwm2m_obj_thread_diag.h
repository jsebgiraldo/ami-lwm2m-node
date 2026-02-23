/*
 * LwM2M Object 33000 — Thread Network Diagnostics
 *
 * Custom object for Thread mesh network monitoring.
 * Provides RSSI, link quality, MAC counters, role, RLOC16, etc.
 */

#ifndef LWM2M_OBJ_THREAD_DIAG_H
#define LWM2M_OBJ_THREAD_DIAG_H

#define THREAD_DIAG_OBJECT_ID     33000

/* Resource IDs */
#define TD_ROLE_RID               0   /* String: Disabled/Detached/Child/Router/Leader */
#define TD_RLOC16_RID             1   /* U16: routing locator */
#define TD_PARTITION_ID_RID       2   /* U32: Thread partition ID */
#define TD_CHANNEL_RID            3   /* U16: Thread channel */
#define TD_PARENT_RSSI_AVG_RID    4   /* S16: average RSSI to parent (dBm) */
#define TD_PARENT_RSSI_LAST_RID   5   /* S16: last RSSI to parent (dBm) */
#define TD_PARENT_LQI_RID         6   /* U8:  link quality indicator (0-3) */
#define TD_PARENT_RLOC16_RID      7   /* U16: parent RLOC16 */
#define TD_TX_TOTAL_RID           8   /* U32: MAC total TX */
#define TD_RX_TOTAL_RID           9   /* U32: MAC total RX */
#define TD_TX_UNICAST_RID         10  /* U32: MAC TX unicast */
#define TD_RX_UNICAST_RID         11  /* U32: MAC RX unicast */
#define TD_TX_BROADCAST_RID       12  /* U32: MAC TX broadcast */
#define TD_RX_BROADCAST_RID       13  /* U32: MAC RX broadcast */
#define TD_TX_ERR_ABORT_RID       14  /* U32: MAC TX errors (abort) */
#define TD_RX_ERR_NOFRAME_RID     15  /* U32: MAC RX errors (no frame) */

#define TD_NUM_FIELDS             16
#define TD_RES_INST_COUNT         16

#endif /* LWM2M_OBJ_THREAD_DIAG_H */
