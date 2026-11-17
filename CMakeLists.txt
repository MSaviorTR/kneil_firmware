cmake_minimum_required(VERSION 3.20.0)

# The kneil_tinyml library (src/kneil_*.c, include/, zephyr/module.yml) is in this folder,
# so this folder is also its Zephyr module. The library builds only if CONFIG_KNEIL_TINYML=y.
list(APPEND ZEPHYR_EXTRA_MODULES ${CMAKE_CURRENT_SOURCE_DIR})

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

project(neilv2)

if(CONFIG_NEIL_APP_KNEIL_LIVE)
    # KNEIL live build (kneil.conf): activity classifier and live stream over BLE
    target_sources(app PRIVATE
        src/kneil_live.c
        src/bma580_fifo.c
        protocol/kneil_proto.c
    )
    target_include_directories(app PRIVATE protocol)

    # Model size for the INFO lines, counted in the generated headers (never edited): a tree with
    # k decisions has 2k+1 nodes. CMake runs again if a model header changes.
    foreach(model forest tree)
        set(model_h ${CMAKE_CURRENT_SOURCE_DIR}/include/kneil_model_${model}.h)
        file(STRINGS ${model_h} decisions REGEX "if \\(features\\[")
        file(STRINGS ${model_h} trees REGEX "int32_t kneil_model_tree_[0-9]+\\(")
        list(LENGTH decisions n_decisions)
        list(LENGTH trees n_trees)
        math(EXPR n_nodes "2 * ${n_decisions} + ${n_trees}")
        string(TOUPPER ${model} MODEL)
        target_compile_definitions(app PRIVATE KNEIL_${MODEL}_TREES=${n_trees} KNEIL_${MODEL}_NODES=${n_nodes})
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${model_h})
    endforeach()
else()
    # NeiL BLE sensor app
    target_sources(app PRIVATE
        src/main.c
        src/rtc.c
        src/bma580.c
        src/TEMP118.c
    )
endif()

target_include_directories(app PRIVATE src)
