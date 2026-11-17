cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

project(kneil_bridge)

# The protocol code is shared with the KNEIL firmware: one place to change it.
set(KNEIL_PROTO_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../protocol)

target_sources(app PRIVATE
    src/main.c
    ${KNEIL_PROTO_DIR}/kneil_proto.c
)
target_include_directories(app PRIVATE ${KNEIL_PROTO_DIR})
