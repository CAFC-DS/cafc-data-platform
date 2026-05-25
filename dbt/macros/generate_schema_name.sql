{#
  Custom generate_schema_name.
  - On target.name == 'prod':           use the +schema config exactly (CORE, APP_COMPAT, …).
  - On any other target (dev, ci, etc): prefix +schema in front of target.schema, e.g.
                                         +schema=CORE, target.schema=DEV_HUMARJI → CORE_DEV_HUMARJI.
  Drop-in replacement for dbt's default; lets the same model code work in
  dev and prod without per-environment overrides.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}

    {%- set default_schema = target.schema -%}

    {%- if custom_schema_name is none -%}
        {{ default_schema }}

    {%- elif target.name == 'prod' -%}
        {{ custom_schema_name | trim }}

    {%- else -%}
        {{ custom_schema_name | trim }}_{{ default_schema }}

    {%- endif -%}

{%- endmacro %}
