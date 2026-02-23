/*
 * LwM2M Object 33000 — Thread MAC Diagnostics (Reduced)
 *
 * Custom object for Thread MAC-layer counters and device state
 * NOT covered by standard OMA objects 10483/10485.
 *
 * v2.0 — Reduced: RLOC16, Channel, Parent info moved to 10483/10485.
 * Remaining: Thread Role, Partition ID, and 8 MAC counters.
 */

#ifndef LWM2M_OBJ_THREAD_DIAG_H
#define LWM2M_OBJ_THREAD_DIAG_H

#define THREAD_DIAG_OBJECT_ID     33000

/* Resource IDs (renumbered for v2.0) */
#define TD_ROLE_RID               0   /* String: Disabled/Detached/Child/Router/Leader */
#define TD_PARTITION_ID_RID       1   /* U32: Thread partition ID */
#define TD_TX_TOTAL_RID           2   /* U32: MAC total TX */
#define TD_RX_TOTAL_RID           3   /* U32: MAC total RX */
#define TD_TX_UNICAST_RID         4   /* U32: MAC TX unicast */
#define TD_RX_UNICAST_RID         5   /* U32: MAC RX unicast */
#define TD_TX_BROADCAST_RID       6   /* U32: MAC TX broadcast */
#define TD_RX_BROADCAST_RID       7   /* U32: MAC RX broadcast */
#define TD_TX_ERR_ABORT_RID       8   /* U32: MAC TX errors (abort) */
#define TD_RX_ERR_NOFRAME_RID     9   /* U32: MAC RX errors (no frame) */

#define TD_NUM_FIELDS             10
#define TD_RES_INST_COUNT         10

#endif /* LWM2M_OBJ_THREAD_DIAG_H */
