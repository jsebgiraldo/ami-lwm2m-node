/*
 * LwM2M Object 10485 — Thread Neighbor Information
 *
 * Standard OMA object (Hydro-Québec, 2023) for Thread neighbor
 * diagnostics. Multiple instances, one per neighbor.
 */

#ifndef LWM2M_OBJ_THREAD_NEIGHBOR_H
#define LWM2M_OBJ_THREAD_NEIGHBOR_H

#define THREAD_NEIGHBOR_OBJECT_ID  10485

/* Resource IDs */
#define NI_ROLE_RID               0   /* Integer: 0=Child, 1=Router */
#define NI_RLOC16_RID             1   /* String: RLOC16 Address */
#define NI_AGE_RID                2   /* Integer: seconds since last contact */
#define NI_AVG_RSSI_RID           3   /* Integer: Average RSSI (dBm) */
#define NI_LAST_RSSI_RID          4   /* Integer: Last RSSI (dBm) */
#define NI_RX_ON_IDLE_RID         5   /* Boolean: Rx On When Idle */
#define NI_FTD_RID                6   /* Boolean: Full Thread Device */
#define NI_FND_RID                7   /* Boolean: Full Network Data */
#define NI_EXT_MAC_RID            8   /* String: Extended MAC Address */
#define NI_LQI_IN_RID             9   /* Integer: Link Quality In (0-3) */
#define NI_LQI_OUT_RID            10  /* Integer: Link Quality Out (0-3) */
#define NI_FRAME_ERR_RID          11  /* Float: Frame error percentage */
#define NI_MSG_ERR_RID            12  /* Float: Message error percentage */
#define NI_QUEUED_MSGS_RID        13  /* Integer: Queued message count */

#define NI_NUM_FIELDS             14
#define NI_MAX_INSTANCES          4   /* Max neighbors tracked */

void init_thread_neighbor_object(void);
void update_thread_neighbors(void);

#endif /* LWM2M_OBJ_THREAD_NEIGHBOR_H */
