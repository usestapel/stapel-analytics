# `schemas/consumes/` — what this module listens to

These files are **documentation of contracts owned elsewhere**, not
registered schemas: `stapel_core.comm.schemas.autoload_schemas` registers
`emits/` and `functions/` only. They are committed so an integrator can read
what stapel-analytics expects on the wire without grepping another repo.

The GDPR trio is subscribed by `stapel_core.gdpr.register_gdpr_owner`
(`apps.py`), which brings its own loose payload schemas — that is why this
directory duplicates none of the registration.

The **comm bridge** (`STAPEL_ANALYTICS["COMM_BRIDGE"]`) consumes host
Actions whose names are unknown at release time, so they cannot appear here.
Their schemas belong to the modules that emit them.
