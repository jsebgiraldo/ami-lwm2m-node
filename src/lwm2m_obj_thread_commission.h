/*
 * LwM2M Object 10484 — Thread Commissioning (joiner add)
 *
 * Standard OMA object (Hydro-Québec, 2023) for commissioning
 * Thread devices onto the network.
 */

#ifndef LWM2M_OBJ_THREAD_COMMISSION_H
#define LWM2M_OBJ_THREAD_COMMISSION_H

#define THREAD_COMMISSION_OBJECT_ID  10484

/* Resource IDs */
#define TC_JOINER_EUI64_RID      0   /* String RW: Joiner EUI64 / discerner */
#define TC_JOINER_PSK_RID        1   /* String RW: Joiner PSK */
#define TC_START_RID             2   /* Execute: Start commissioning */
#define TC_CANCEL_RID            3   /* Execute: Cancel commissioning */
#define TC_STATE_RID             4   /* Integer R: 0=Disabled, 1=Active */
#define TC_PENDING_IDS_RID       5   /* String R Multi: Pending joiner IDs */

#define TC_NUM_FIELDS            6

void init_thread_commission_object(void);

#endif /* LWM2M_OBJ_THREAD_COMMISSION_H */
