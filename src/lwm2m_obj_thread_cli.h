/*
 * LwM2M Object 10486 — Thread CLI Command
 *
 * Standard OMA object (Hydro-Québec, 2023) for issuing
 * CLI commands to Thread devices remotely.
 */

#ifndef LWM2M_OBJ_THREAD_CLI_H
#define LWM2M_OBJ_THREAD_CLI_H

#define THREAD_CLI_OBJECT_ID     10486

/* Resource IDs */
#define TCLI_VERSION_RID         0   /* String R: CLI version */
#define TCLI_COMMAND_RID         1   /* String RW: Command to execute */
#define TCLI_EXECUTE_RID         2   /* Execute: Send command */
#define TCLI_RESULT_RID          3   /* String R: Command result */

#define TCLI_NUM_FIELDS          4

void init_thread_cli_object(void);

#endif /* LWM2M_OBJ_THREAD_CLI_H */
