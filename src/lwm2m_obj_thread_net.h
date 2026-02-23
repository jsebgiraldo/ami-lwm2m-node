/*
 * LwM2M Object 10483 — Thread Network
 *
 * Standard OMA object (Hydro-Québec, 2023) for Thread network
 * configuration and identity information.
 */

#ifndef LWM2M_OBJ_THREAD_NET_H
#define LWM2M_OBJ_THREAD_NET_H

#define THREAD_NET_OBJECT_ID     10483

/* Resource IDs */
#define TN_NET_NAME_RID          0   /* String: Thread network name */
#define TN_PAN_ID_RID            1   /* String: PAN ID */
#define TN_XPAN_ID_RID           2   /* String: Extended PAN ID (Multi) */
#define TN_PASSPHRASE_RID        3   /* String: Passphrase (optional) */
#define TN_MASTER_KEY_RID        4   /* String: Master Key (optional) */
#define TN_CHANNEL_RID           5   /* Integer: IEEE 802.15.4 Channel */
#define TN_MESH_PREFIX_RID       6   /* String: IPv6 Mesh Local Prefix */
#define TN_MAX_CHILDREN_RID      7   /* Integer: Max children */
#define TN_RLOC16_RID            8   /* String: RLOC16 Address */
#define TN_EUI64_RID             9   /* String: EUI64 */
#define TN_EXT_MAC_RID           10  /* String: Extended MAC Address */
#define TN_IPV6_ADDRS_RID        11  /* String: IPv6 Addresses (Multi) */

#define TN_NUM_FIELDS            12
#define TN_MAX_IPV6              4   /* Max IPv6 addresses */

void init_thread_net_object(void);
void update_thread_network(void);

#endif /* LWM2M_OBJ_THREAD_NET_H */
