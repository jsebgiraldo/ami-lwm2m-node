# Unit Tests — AMI LwM2M Node

Tests unitarios portables para las capas de lógica pura del firmware.

## Módulos testeados

| Módulo | Archivo test | Qué prueba |
|--------|-------------|------------|
| HDLC | `test_hdlc.c` | CRC-16, build SNRM/DISC/I-frame, frame parse/find |
| COSEM | `test_cosem.c` | AARQ build, AARE parse, GET req/resp, data decode |
| DLMS Meter | `test_dlms_logic.c` | value_to_double, OBIS table, struct offsets |

## Cómo compilar y ejecutar

```powershell
cd tests
gcc -o run_tests.exe test_main.c test_hdlc.c test_cosem.c test_dlms_logic.c ^
    ../src/dlms_hdlc.c ../src/dlms_cosem.c ^
    -I../src -Istubs -DUNIT_TEST -lm
.\run_tests.exe
```

## Arquitectura

Los tests usan **stubs** ligeros que reemplazan las APIs de Zephyr (`LOG_*`,
`k_uptime_get`, etc.) para que el código compile nativamente sin Zephyr SDK.

```
tests/
├── stubs/
│   └── zephyr_stubs.h   ← Stubs para Zephyr kernel, logging, errno
├── test_framework.h      ← Mini-framework assert (sin dependencias)
├── test_main.c           ← Entry point: ejecuta todos los test suites
├── test_hdlc.c           ← Tests HDLC layer
├── test_cosem.c          ← Tests COSEM layer
├── test_dlms_logic.c     ← Tests lógica DLMS meter
└── README.md
```
