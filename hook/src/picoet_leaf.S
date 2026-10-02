// picoet_leaf.S -- Hook B leaf template (P4 dual).
//
// Copied at runtime into the phoenix plugin's zero tail page (base+0x494c58),
// which lies inside the plugin's executable mapping. The template is position
// independent; before the copy is written to the plugin two spots are patched:
//   picoet_leaf_g_slot -> absolute address of picoet_g (our .bss)
//   picoet_leaf_ret_pc -> relative b back to base+0x5c6d0
// Register use (x11-x14, w12, s16-s19) matches the offline cave-dual.S: all
// are dead at the 0x5c6cc join. No sp changes. x30 is untouched (entered with
// a plain b, left with a plain b).

    .section .text.picoet_leaf, "ax", %progbits
    .globl picoet_leaf_start
    .hidden picoet_leaf_start
    .globl picoet_leaf_end
    .hidden picoet_leaf_end
    .globl picoet_leaf_g_slot
    .hidden picoet_leaf_g_slot
    .globl picoet_leaf_ret_pc
    .hidden picoet_leaf_ret_pc
    .type picoet_leaf_start, %function
    .p2align 2
picoet_leaf_start:
    ldr x11, picoet_leaf_g_slot
    ldar w12, [x11]                // G.valid (acquire)
    cbz w12, .Lleaf_ret
    ldr x13, [x11, #8]             // G.stamp
    ldr x14, [sp, #0x28]           // this frame's timestamp
    cmp x13, x14
    b.ne .Lleaf_ret
    ldrb w12, [sp, #0x60]          // left valid byte
    cbz w12, .Lleaf_right
    ldp s16, s17, [x11, #0x10]     // S_L
    ldp s18, s19, [x11, #0x20]     // F_raw
    fsub s16, s16, s18
    fsub s17, s17, s19
    ldr s18, [sp, #0x64]
    fadd s18, s18, s16
    str s18, [sp, #0x64]           // x += S_L.x - F_raw.x
    ldr s18, [sp, #0x68]
    fadd s18, s18, s17
    str s18, [sp, #0x68]           // y += S_L.y - F_raw.y
    ldr w12, [x11, #0x38]
    add w12, w12, #1
    str w12, [x11, #0x38]          // G.n_fix++
.Lleaf_right:
    ldrb w12, [sp, #0xd0]          // right valid byte
    cbz w12, .Lleaf_clear
    ldp s16, s17, [x11, #0x18]     // S_R
    ldp s18, s19, [x11, #0x20]     // F_raw
    fsub s16, s16, s18
    fsub s17, s17, s19
    ldr s18, [sp, #0xd4]
    fadd s18, s18, s16
    str s18, [sp, #0xd4]           // x += S_R.x - F_raw.x
    ldr s18, [sp, #0xd8]
    fadd s18, s18, s17
    str s18, [sp, #0xd8]           // y += S_R.y - F_raw.y
    ldr w12, [x11, #0x38]
    add w12, w12, #1
    str w12, [x11, #0x38]          // G.n_fix++
.Lleaf_clear:
    str wzr, [x11]                 // once per frame
.Lleaf_ret:
    ldr w10, [sp, #0x24]           // relocated original instruction
picoet_leaf_ret_pc:
    b .                            // patched to base+0x5c6d0
    .p2align 3
picoet_leaf_g_slot:
    .quad 0                        // patched to &picoet_g
picoet_leaf_end:
    .size picoet_leaf_start, .-picoet_leaf_start
