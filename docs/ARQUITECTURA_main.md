# Arquitectura de `main.c` — guía visual para entender qué hace el código

> Objetivo: entender el flujo del firmware AMI antes de decidir qué optimizar.
> Todos los diagramas son Mermaid (se renderizan en VSCode / GitHub).

`main.c` (3400 líneas) mezcla 8 subsistemas. Esta guía los separa visualmente.

---

## 0. Vista de 10.000 pies — ¿qué es este firmware?

Un nodo AMI: lee un **medidor eléctrico** por RS-485/DLMS, se une a una **malla Thread**,
y publica la telemetría a **ThingsBoard Edge** vía **LwM2M** (CoAP). Todo lo demás
(watchdogs, recovery, LED, shell, diag) existe para que eso **no se caiga** en una
flota de 30 nodos.

```mermaid
flowchart LR
    MED["Medidor eléctrico<br/>(RS-485 / DLMS-COSEM)"] -->|"poll cada Ns"| NODO
    subgraph NODO["Nodo ESP32-C6 (este firmware)"]
      direction TB
      DLMS["dlms_thread<br/>lee OBIS"] --> OBJ["Objetos LwM2M<br/>10242 power, 33000 diag, 33001 thread"]
      OBJ --> ENG["Engine LwM2M<br/>(CoAP)"]
    end
    ENG -->|"REGISTER / NOTIFY<br/>sobre Thread"| OTBR["Pi4 OTBR<br/>(border router)"]
    OTBR -->|"observe / RPC"| TB["ThingsBoard Edge<br/>192.168.8.111"]
```

---

## 1. Secuencia de boot — `main()` paso a paso

Lo más importante: **el orden de arranque es deliberado**. Cada capa de defensa
(watchdog) se arma ANTES de la operación riesgosa que protege.

```mermaid
flowchart TD
    PK["SYS_INIT @POST_KERNEL<br/>hw_watchdog_boot_arm()<br/>arma chan_boot SIN alimentar"] --> M0["main() entry"]
    M0 --> C1["ami_reboot_capture_boot_code()<br/>latch RID37 = por qué rebooteó este boot"]
    C1 --> ST["BOOT STAGGER<br/>sleep 0..30s aleatorio<br/>(evita que N boards re-arranquen sincronizados)"]
    ST --> SET["settings_subsys_init()<br/>load NVS: ami/* + pm/*"]
    SET --> RR["capture_reset_reason()<br/>persiste total_resets (RID22)"]
    RR --> BB{"boot_burst >= MAX?<br/>(crash-loop?)"}
    BB -- "sí" --> THR["throttle: sleep largo<br/>(protege NVS de wear + ventana p/ intervenir)"]
    BB -- "no" --> INC["inc boot_burst"]
    THR --> INC
    INC --> PM["publica post-mortem<br/>del boot ANTERIOR (RIDs 23-28)"]
    PM --> HW["hw_watchdog_init()<br/>PRIO9: chan_kernel + chan_workq"]
    HW --> SURV["hw_watchdog_note_boot_survived()<br/>alimenta chan_boot = 'pasé la zona NVS'"]
    SURV --> BW["boot_watchdog ARM<br/>PRIO8: deadline a 1er REGISTER"]
    BW --> DS["apply_otbr_dataset()<br/>TLV: prefijo mesh + PSKc + canal 25"]
    DS --> CB["registra thread_state_changed_cb"]
    CB --> POLL{"poll role hasta<br/>Child/Router/Leader<br/>(max 120 x 2s)"}
    POLL -- "attached" --> IPV6["LED CYAN + espera 5s<br/>(propagación IPv6)"]
    IPV6 --> EP["build_endpoint_name()<br/>ami-esp32c6-XXXX desde MAC"]
    EP --> DNS{"lwm2m_discover_with_retry()<br/>DNS-SD x10"}
    DNS -- "falla terminal" --> WR["warm-reboot<br/>(dns-sd-boot-fail)"]
    DNS -- "ok" --> SU["lwm2m_setup()<br/>registra objetos LwM2M"]
    SU --> JIT["REGISTER jitter 0..30s<br/>(anti server-avalanche)"]
    JIT --> RN["reached_network=1<br/>(boot-wd ya NO rebootea: fallo ahora es server-side)"]
    RN --> START["lwm2m_rd_client_start()<br/>arranca REGISTER"]
    START --> W5["lwm2m_watchdog_init() PRIO5"]
    W5 --> KA["coap_keepalive_init() PRIO9"]
    KA --> AW["arma mesh_alone_wdog + conn_monitor_wdog (60s)"]
    AW --> LOOP[["MAIN LOOP (abajo)"]]

    style PK fill:#fde,stroke:#a33
    style HW fill:#fde,stroke:#a33
    style BW fill:#fde,stroke:#a33
    style W5 fill:#fde,stroke:#a33
    style KA fill:#fde,stroke:#a33
    style AW fill:#fde,stroke:#a33
```

> 🔴 Los nodos rojos son **las capas de watchdog** — fijate cómo se arman
> progresivamente a medida que el boot avanza.

---

## 2. El MAIN LOOP — lo que corre para siempre

Sorprendentemente simple: el loop solo **dispara** trabajo, no lo hace él mismo.

```mermaid
flowchart TD
    L0["while(1): k_sleep(LOOP_TICK)"] --> D1{"¿toca DLMS poll?<br/>(dlms_poll_interval_s)"}
    D1 -- "sí" --> D2["k_sem_give(dlms_poll_sem)<br/>despierta el dlms_thread (no bloquea)"]
    D1 -- "no" --> C1
    D2 --> C1{"¿toca conn update?<br/>(60s + MAC phase + jitter)"}
    C1 -- "sí" --> C2["update_connectivity_metrics()<br/>update_thread_network()<br/>update_thread_neighbors()<br/>thread_role_refresh()"]
    C2 --> C3["ami_conn_monitor_note_tick()<br/>= alimenta el conn_monitor watchdog"]
    C3 --> L0
    C1 -- "no" --> L0
```

> El loop principal late cada `LOOP_TICK`. Si **deja** de latir (se cuelga), el
> `conn_monitor_wdog` lo detecta porque `note_tick()` no se llama → COLD reboot.

---

## 3. Concurrencia — qué corre EN PARALELO después del boot

Esto es lo que más confunde: hay **muchos hilos/works** corriendo a la vez.

```mermaid
flowchart TB
    subgraph hilos["Hilos y works concurrentes"]
      direction LR
      ML["main loop<br/>(conn metrics)"]
      DT["dlms_thread<br/>(RS-485 poll)"]
      KAT["coap_keepalive PRIO9<br/>notify /33000/0/10"]
      SWT["lwm2m_watchdog PRIO7<br/>(silence check 60s)"]
      HWT["hw_wdog thread PRIO4<br/>(feed cada 60s)"]
      EVT["rd_client_event<br/>(callback del engine)"]
      RWT["recover_work<br/>(system_workq)"]
    end

    DT -->|"meter_readings"| OBJW["objetos LwM2M<br/>(power/diag/thread)"]
    ML -->|"thread/conn metrics"| OBJW
    OBJW -->|"NOTIFY a observers"| EDGE["TB Edge"]
    KAT -->|"ret>0 = entrega viva"| HWT
    EVT -->|"REGISTER/UPDATE OK"| HWT
    EVT -->|"REGISTER/UPDATE OK"| SWT
    KAT -->|"3 fallos / ENGINE_SUSPENDED"| RWT
    SWT -->|"silencio > umbral"| RWT
    EVT -->|"FAILURE/TIMEOUT/DISCONNECT"| RWT
    RWT -->|"re-REGISTER o COLD reboot"| EVT
```

> Regla de oro del diseño: **una señal de salud solo cuenta si NO se puede
> falsear con la radio muerta.** Por eso los watchdogs se alimentan de eventos
> *server-ACKed* (REGISTER) y de *entrega real* (notify ret>0), no de
> "puse el dato en la cola".

---

## 4. Defensa en profundidad — los 7 watchdogs (¡esto es clave!)

Hay **siete** capas que pueden rebootear/recuperar el nodo. Cada una cubre un
modo de falla que las otras NO ven.

```mermaid
flowchart TD
    subgraph capas["Capas de watchdog (de más blando a más duro)"]
      direction TB
      A["1. boot_watchdog (PRIO8, system_workq)<br/>NO registra en X s tras boot → WARM reboot<br/>se cancela en 1er REGISTER"]
      B["2. lwm2m_watchdog / silence (PRIO7, hilo propio)<br/>sin evento server-ACKed en 3x keepalive → recover_work"]
      C["3. coap_keepalive (PRIO9)<br/>3 fallos de notify → recover_work"]
      D["4. mesh_alone_watchdog (60s)<br/>solo en la malla, sin vecinos → reboot"]
      E["5. conn_monitor_watchdog (60s)<br/>main loop dejó de latir → COLD reboot"]
      F["6. hw_watchdog REG-gate (PRIO4, dependency-free)<br/>sin REGISTER/UPDATE en REAL_LIVENESS_TIMEOUT → COLD"]
      G["7. hw_watchdog DELIVERY-gate (v0.7.9, NUEVO)<br/>observado pero 0 entrega → soft x2 → COLD capado x5"]
      H["8. TG0_WDT (hardware, mask ROM)<br/>si TODO lo anterior se cuelga → SYS_RESET"]
    end
    A --> B --> C --> D --> E --> F --> G --> H
```

| # | Watchdog | Qué modo de falla caza | Acción |
|---|---|---|---|
| 1 | boot (PRIO8) | nunca llega al 1er REGISTER | WARM reboot |
| 2 | silence (PRIO7) | engine sin eventos (zombie) | recover_work |
| 3 | keepalive (PRIO9) | socket CoAP muerto | recover_work |
| 4 | mesh-alone | aislado en la malla | reboot |
| 5 | conn-monitor | main loop colgado | COLD reboot |
| 6 | hw REG-gate (PRIO4) | radio/USB wedge, CPU vivo | COLD (dependency-free) |
| 7 | hw DELIVERY-gate | registrado pero sin entregar telemetría | soft→COLD capado |
| 8 | TG0_WDT (HW) | todo lo de software muerto | SYS_RESET |

> **El #7 es el que acabamos de construir** (delivery-liveness gate). Caza
> exactamente el estado de Lab 17: registrado, REG_UPDATE OK, pero observe-session
> muerta → 0 telemetría por horas, sin auto-reboot.

---

## 5. Máquina de estados de la conexión LwM2M (+ colores del LED)

`rd_client_event()` es el corazón del ciclo de vida. Cada evento del engine
mueve el estado y cambia el LED.

```mermaid
stateDiagram-v2
    [*] --> Boot
    Boot --> WaitThread: main()
    WaitThread --> MeshAttached: role >= Child (LED CYAN)
    MeshAttached --> Registering: rd_client_start (LED azul parpadeo)
    Registering --> Registered: REGISTRATION_COMPLETE (LED GREEN flash → OFF)
    Registered --> Registered: REG_UPDATE_COMPLETE (alimenta watchdogs)
    Registered --> Recovering: DISCONNECT (LED YELLOW)
    Registering --> Recovering: FAILURE / TIMEOUT (LED RED)
    Registered --> Recovering: NETWORK_ERROR (LED RED, backoff x2)
    Recovering --> Registering: recover_work re-REGISTER
    Recovering --> ColdReboot: > MAX_ATTEMPTS (10)
    ColdReboot --> Boot: SYS_REBOOT_COLD

    note right of Registered
      En este estado el delivery-gate
      vigila que la telemetria fluya
      (notify ret>0). Si no, escala.
    end note
```

**Colores del LED** (señal visual de campo):
| Color | Estado |
|---|---|
| OFF | early boot / idle operando (solo parpadea en cada TX) |
| BLUE (parpadeo) | esperando attach a Thread |
| CYAN | malla attached, esperando IPv6 |
| GREEN (flash) | REGISTER completo → entra a operación |
| YELLOW | desconectado |
| RED | fallo de registro / network error |

> Nota: en producción el LED está **no-op'd** (GPIO8 rompe la radio SPI). Ver
> `project_spi_breaks_thread_radio`. El subsistema LED (~300 líneas) es candidato
> #1 a recortar.

---

## 6. Máquina de recovery — `lwm2m_recover_work_fn()`

Cómo el nodo intenta recuperarse, con backoff y escalación a reboot.

```mermaid
flowchart TD
    EV["evento de fallo<br/>(FAILURE/TIMEOUT/DISCONNECT/NETWORK_ERR<br/>o keepalive/silence/delivery)"] --> RW["recover_work_fn"]
    RW --> RE{"¿ya recuperando?<br/>(re-entry guard)"}
    RE -- "sí" --> X1["return (se re-disparará)"]
    RE -- "no" --> MESH{"¿Thread attached?"}
    MESH -- "no" --> X2["defer: esperar eager re-attach<br/>(no quemar intentos sin malla)"]
    MESH -- "sí" --> ATT["attempt++"]
    ATT --> MAX{"attempt > MAX(10)?"}
    MAX -- "sí" --> COLD["ami_reboot_drain(COLD)<br/>'max-recover-attempts'"]
    MAX -- "no" --> DNS{"re-DNS-SD ok?"}
    DNS -- "no" --> RETRY["backoff + reschedule"]
    DNS -- "sí" --> RS["rd_client_stop + memset ctx<br/>+ rd_client_start"]
    RS --> OK{"start ok?"}
    OK -- "sí" --> PROBE["probe en LWM2M_RECOVER_PROBE_S<br/>(detecta 'start ok pero nunca registró')"]
    OK -- "no" --> RETRY
    PROBE --> PCHK{"reg_success subió?"}
    PCHK -- "sí" --> DONE["recuperado ✓"]
    PCHK -- "no" --> RETRY
    RETRY --> RW
```

> Detalles finos: backoff **exponencial** + jitter; `NETWORK_ERROR` duplica el
> backoff (storm-backoff); tras MAX escala a **COLD** (no WARM) porque WARM
> preservaba la corrupción de mesh-attach. El `noreg_boots` evita reboot-storms
> en outage de servidor.

---

## 7. Flujo de datos — del medidor a ThingsBoard

```mermaid
sequenceDiagram
    participant ML as main loop
    participant DT as dlms_thread
    participant MET as Medidor (RS-485)
    participant OBJ as Objetos LwM2M
    participant ENG as Engine CoAP
    participant TB as TB Edge

    ML->>DT: k_sem_give(dlms_poll_sem)
    DT->>MET: DLMS/COSEM read OBIS
    MET-->>DT: voltage, power, energy...
    DT->>OBJ: lwm2m_set_f64(/10242/0/...)
    OBJ->>ENG: NOTIFY (si cambió > umbral)
    ENG->>TB: CoAP CON (observe notification)
    Note over ML,TB: en paralelo, cada 60s el conn loop<br/>actualiza /33000 (diag) y /33001 (thread)
    ML->>OBJ: update_connectivity_metrics()
    OBJ->>ENG: NOTIFY /33000/0/...
    ENG->>TB: telemetría diag
```

---

## 8. Mapa de módulos — propuesta de refactor (split de main.c)

Cómo quedaría si separamos `main.c` en módulos (cero cambio de comportamiento).

```mermaid
flowchart TB
    subgraph actual["HOY: main.c = 3400 líneas"]
      MAIN0["TODO junto"]
    end
    subgraph propuesto["PROPUESTO"]
      direction TB
      MAINc["main.c (~1200)<br/>boot + main loop + glue"]
      SHELL["ami_shell.c (~630)<br/>25 comandos shell"]
      DIAG["ami_diag.c (~330)<br/>contadores Object 33000 + NVS"]
      LEDc["ami_led.c (~300)<br/>RGB/brightness (¿recortar?)"]
      REBOOT["ami_reboot.c (~330)<br/>drain + panic + reset-cause"]
      RECOV["ami_recovery.c (~430)<br/>recover_work + 3 watchdogs"]
    end
    actual ==> propuesto
```

**Candidatos a RECORTAR (no solo mover):**
- 🟡 `fill_demo_readings` (~80 líneas): solo bajo `CONFIG_AMI_DEMO_MODE` (off en prod) pero se compila siempre → `#ifdef` o borrar.
- 🟡 `cmd_ami_test_*` (~250 líneas): comandos de test puro dev → gate tras `CONFIG_AMI_TEST_CMDS`.
- 🟡 8 comandos `cmd_ami_log_*` → colapsar en 1 parametrizado.
- 🟡 Subsistema LED (~300 líneas): ya está no-op'd en HW → ¿lo dejamos como stub mínimo?

---

## Resumen para decidir

1. **Entender** (este doc) ✓
2. **Split** (refactor seguro): main.c → 6 módulos, queda en ~1200 líneas.
3. **Trim** (recorte real de flash/RAM): demo + test-cmds + LED, con test.
4. **Lo confuso**: los 7 watchdogs (sección 4) y la recovery (sección 6) son lo
   más denso — empezá por ahí si algo no cierra.
