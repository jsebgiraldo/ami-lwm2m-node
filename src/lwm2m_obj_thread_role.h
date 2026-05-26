#ifndef LWM2M_OBJ_THREAD_ROLE_H
#define LWM2M_OBJ_THREAD_ROLE_H

/* Object 33001 — Thread Role Control. See lwm2m_obj_thread_role.c for the
 * resource map. Call init at LwM2M-object configuration time; call refresh
 * periodically (e.g. from the existing connectivity-monitor loop) so the
 * /33001/0/4 current_role attribute tracks live OT state. */
int  thread_role_init(void);
void thread_role_refresh(void);

#endif /* LWM2M_OBJ_THREAD_ROLE_H */
