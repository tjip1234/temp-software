# TJIP-1 reference codec

Portable C11 implementation of the wire protocol in `../docs/protocol.md`. No
dynamic allocation, no dependencies beyond `<string.h>`, `<stdint.h>` and
`<stdio.h>` (the last only for `snprintf` in `tjip_encode_error`).

```
tjip_proto.h        the API
tjip_proto.c        the implementation
test_tjip_proto.c   self-tests and the cross-check vector generator
Makefile            make test · make crosscheck
```

## Using it in ESP-IDF

Drop both files into a component:

```
components/tjip_proto/
├── CMakeLists.txt
├── tjip_proto.c
└── include/tjip_proto.h
```

```cmake
idf_component_register(SRCS "tjip_proto.c" INCLUDE_DIRS "include")
```

It builds unchanged. Nothing in it is Espressif-specific, so the same files work
if you ever move the design to another MCU.

## Receiving

```c
static tjip_reader_t reader;
static uint8_t rx_scratch[TJIP_MAX_FRAME];

void app_main(void) {
    tjip_reader_init(&reader, rx_scratch, sizeof rx_scratch);
}

void on_usb_rx(const uint8_t *data, size_t len) {
    tjip_frame_t frame;
    size_t consumed;
    while (tjip_reader_feed(&reader, data, len, &consumed, &frame)) {
        handle_request(&frame);
        data += consumed;
        len  -= consumed;
    }
}
```

The reader never raises an error on corrupt input: bad blocks are counted in
`reader.bad_frames` and skipped, and it resynchronises on the next delimiter.
That matters because the first bytes after a USB CDC port opens are routinely
garbage, and because you do not want a single bit flip to wedge acquisition.

`frame.payload` points into your scratch buffer and is valid only until the next
call to `tjip_reader_feed`. Copy it if you need it longer.

## Sending a sample block

```c
static const uint8_t channel_ids[] = {
    TJIP_CH_PT1000, TJIP_CH_TYPEK, TJIP_CH_TYPEK_CJ,
    TJIP_CH_NTC_EXT1, TJIP_CH_NTC_EXT2, TJIP_CH_NTC_EXT3,
    TJIP_CH_NTC_BRD_RTD, TJIP_CH_NTC_BRD_TC, TJIP_CH_NTC_BRD_CHG,
    TJIP_CH_V_BAT, TJIP_CH_V_CC, TJIP_CH_AHT20_T, TJIP_CH_AHT20_RH,
    TJIP_CH_PT1000_R, TJIP_CH_TYPEK_UV,
};

uint8_t payload[TJIP_MAX_PAYLOAD];
tjip_block_header_t header = {
    .first_seq   = ring_first_seq,
    .t0_us       = esp_timer_get_time(),
    .dt_us       = 10000,
    .fault_mask  = current_fault_mask,
    .seq_step    = 1,
    .n_ch        = sizeof channel_ids,
    .n_samples   = rows,
    .channel_ids = channel_ids,
};

size_t at = tjip_block_write_header(&header, payload, sizeof payload);
for (uint16_t r = 0; r < rows; ++r) {
    at += tjip_block_write_row(row_values(r), header.n_ch,
                               payload + at, sizeof payload - at);
}

uint8_t wire[TJIP_MAX_FRAME];
size_t n = tjip_encode(TJIP_MSG_SAMPLE_BLOCK, 0, 0, payload, at, wire, sizeof wire);
transport_write(wire, n);
```

Use `tjip_block_rows_for(n_ch, budget)` to decide how many rows fit before you
start. For 15 channels in the 4096-byte payload the answer is 67, which at 100 Hz
is a block roughly every 670 ms.

A reading that is invalid goes on the wire as **NaN**, and the corresponding bit
in `fault_mask` says which sensor to blame. Do not substitute zero, and do not
omit the row — the host uses NaN to draw the gap and to leave the cell empty on
export.

## Things easy to get wrong

**Read the device clock late.** In `TIME_ECHO`, everything between reading `t3`
and the bytes actually leaving the wire shows up as asymmetry, and asymmetry is
the one error an SNTP-style exchange cannot detect or correct. Read it as the
last thing before handing the frame to the transport.

**Sequence numbers count acquisitions, not transmissions.** They increment once
per acquired sample and never reset except on reboot, which the host sees as
`first_seq` going backwards together with a changed `STATUS.boot_id`. If you
thin the stream for a slow link, set `seq_step` and the `DECIMATED` flag —
`tjip_block_write_header` sets the flag for you when `seq_step != 1`. Without it
the host would open a gap for every skipped row and then try to backfill them
over the very link that was too slow to carry them.

**One acquisition schedule, many hosts.** `STREAM_START.rate_hz` is a ceiling on
what that host wants, not a private sampling rate. Sending everything is normally
right; 15 channels at 100 Hz is about 6 kB/s.

**Calibration is evaluated on the board.** The host fits and sends coefficients;
the board applies them, so its own display and every connected program show the
same number. `SET_CAL` must be atomic — validate the whole object, write NVS, then
reply. On failure write nothing and return `ERROR`.

## Verifying against the host

```sh
make test        # C self-tests: CRC vector, COBS, framing, resync, blocks
make crosscheck  # byte-for-byte agreement with the Python implementation
```

`crosscheck` builds the same set of frames on both sides and diffs the hex. If it
passes, your firmware and the desktop software cannot disagree about the wire —
which is a stronger guarantee than any amount of prose in the specification.
