//! Dependency-free content identity: FIPS 180-4 SHA-256 and canonical JSON text.
//!
//! The kernel must be able to verify a strategy artifact's digests from
//! repository bytes instead of trusting a caller's declared strings. Pulling a
//! hashing crate for one function would add a dependency for no new capability,
//! so the algorithm lives here with published test vectors.
//!
//! No IO, no clock, no randomness.

const K: [u32; 64] = [
    0x428a_2f98, 0x7137_4491, 0xb5c0_fbcf, 0xe9b5_dba5, 0x3956_c25b, 0x59f1_11f1, 0x923f_82a4, 0xab1c_5ed5,
    0xd807_aa98, 0x1283_5b01, 0x2431_85be, 0x550c_7dc3, 0x72be_5d74, 0x80de_b1fe, 0x9bdc_06a7, 0xc19b_f174,
    0xe49b_69c1, 0xefbe_4786, 0x0fc1_9dc6, 0x240c_a1cc, 0x2de9_2c6f, 0x4a74_84aa, 0x5cb0_a9dc, 0x76f9_88da,
    0x983e_5152, 0xa831_c66d, 0xb003_27c8, 0xbf59_7fc7, 0xc6e0_0bf3, 0xd5a7_9147, 0x06ca_6351, 0x1429_2967,
    0x27b7_0a85, 0x2e1b_2138, 0x4d2c_6dfc, 0x5338_0d13, 0x650a_7354, 0x766a_0abb, 0x81c2_c92e, 0x9272_2c85,
    0xa2bf_e8a1, 0xa81a_664b, 0xc24b_8b70, 0xc76c_51a3, 0xd192_e819, 0xd699_0624, 0xf40e_3585, 0x106a_a070,
    0x19a4_c116, 0x1e37_6c08, 0x2748_774c, 0x34b0_bcb5, 0x391c_0cb3, 0x4ed8_aa4a, 0x5b9c_ca4f, 0x682e_6ff3,
    0x748f_82ee, 0x78a5_636f, 0x84c8_7814, 0x8cc7_0208, 0x90be_fffa, 0xa450_6ceb, 0xbef9_a3f7, 0xc671_78f2,
];

const INITIAL: [u32; 8] = [
    0x6a09_e667, 0xbb67_ae85, 0x3c6e_f372, 0xa54f_f53a,
    0x510e_527f, 0x9b05_688c, 0x1f83_d9ab, 0x5be0_cd19,
];

/// Lowercase SHA-256 hex digest of the exact bytes supplied.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut state = INITIAL;
    let bit_length = (bytes.len() as u64).wrapping_mul(8);
    let mut tail = Vec::with_capacity(bytes.len() + 72);
    tail.extend_from_slice(bytes);
    tail.push(0x80);
    while tail.len() % 64 != 56 {
        tail.push(0);
    }
    tail.extend_from_slice(&bit_length.to_be_bytes());
    for block in tail.chunks_exact(64) {
        let mut w = [0u32; 64];
        for (index, word) in block.chunks_exact(4).enumerate() {
            w[index] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for index in 16..64 {
            let s0 = w[index - 15].rotate_right(7) ^ w[index - 15].rotate_right(18) ^ (w[index - 15] >> 3);
            let s1 = w[index - 2].rotate_right(17) ^ w[index - 2].rotate_right(19) ^ (w[index - 2] >> 10);
            w[index] = w[index - 16].wrapping_add(s0).wrapping_add(w[index - 7]).wrapping_add(s1);
        }
        let mut work = state;
        for index in 0..64 {
            let s1 = work[4].rotate_right(6) ^ work[4].rotate_right(11) ^ work[4].rotate_right(25);
            let choice = (work[4] & work[5]) ^ ((!work[4]) & work[6]);
            let temp1 = work[7].wrapping_add(s1).wrapping_add(choice)
                .wrapping_add(K[index]).wrapping_add(w[index]);
            let s0 = work[0].rotate_right(2) ^ work[0].rotate_right(13) ^ work[0].rotate_right(22);
            let majority = (work[0] & work[1]) ^ (work[0] & work[2]) ^ (work[1] & work[2]);
            let temp2 = s0.wrapping_add(majority);
            work[7] = work[6];
            work[6] = work[5];
            work[5] = work[4];
            work[4] = work[3].wrapping_add(temp1);
            work[3] = work[2];
            work[2] = work[1];
            work[1] = work[0];
            work[0] = temp1.wrapping_add(temp2);
        }
        for index in 0..8 {
            state[index] = state[index].wrapping_add(work[index]);
        }
    }
    state.iter().map(|word| format!("{word:08x}")).collect()
}

/// Canonical JSON text: compact separators, object keys sorted.
///
/// `serde_json`'s default map is a `BTreeMap`, so re-serialising any `Value`
/// yields sorted keys and no whitespace — the same text the Python producer
/// builds with `sort_keys=True, separators=(",", ":")`.
pub fn canonical_json_text(value: &serde_json::Value) -> String {
    serde_json::to_string(value).expect("a serde_json::Value always serializes")
}

/// Lowercase SHA-256 of the canonical JSON text of `value`.
pub fn canonical_digest(value: &serde_json::Value) -> String {
    sha256_hex(canonical_json_text(value).as_bytes())
}

/// Newline normalization so a CRLF working tree and an LF checkout share one source identity.
pub fn normalize_newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

/// Whether `value` is already canonical JSON text.
pub fn is_canonical_json_text(value: &str) -> bool {
    serde_json::from_str::<serde_json::Value>(value)
        .map(|parsed| canonical_json_text(&parsed) == value)
        .unwrap_or(false)
}
