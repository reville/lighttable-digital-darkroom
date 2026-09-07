//! Full-precision film output for the established Python export finisher.
//!
//! Unlike a display surface, this carries the exact native-endian f32 samples
//! written to a float TIFF. The server adopts and immediately unlinks the name;
//! its read-only mapping stays alive as long as an export or cache uses it.
use anyhow::{Context, Result, bail};
use serde::Serialize;

pub const MAX_BYTES: usize = 1024 * 1024 * 1024;

#[derive(Debug, Serialize)]
pub struct SharedExport {
    pub name: String,
    pub length: usize,
    pub offset: usize,
    pub width: u32,
    pub height: u32,
    #[serde(rename = "rowBytes")]
    pub row_bytes: usize,
    pub format: &'static str,
    #[serde(rename = "byteOrder")]
    pub byte_order: &'static str,
}

#[cfg(unix)]
pub fn publish(width: u32, height: u32, samples: &[f32]) -> Result<SharedExport> {
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let row_bytes = (width as usize).checked_mul(12).context("export row overflow")?;
    let length = row_bytes.checked_mul(height as usize).context("export size overflow")?;
    if width == 0 || height == 0 || length > MAX_BYTES
        || samples.len().checked_mul(4) != Some(length) {
        bail!("invalid or oversized shared export");
    }
    let name = format!("/lte-{:x}-{:x}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed));
    let c_name = std::ffi::CString::new(name.clone())?;
    let fd = unsafe { libc::shm_open(c_name.as_ptr(), libc::O_RDWR | libc::O_CREAT | libc::O_EXCL, 0o600) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).context("creating shared export");
    }
    let result = (|| {
        if unsafe { libc::ftruncate(fd, length as libc::off_t) } != 0 {
            return Err(std::io::Error::last_os_error()).context("sizing shared export");
        }
        let address = unsafe { libc::mmap(std::ptr::null_mut(), length,
            libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, fd, 0) };
        if address == libc::MAP_FAILED {
            return Err(std::io::Error::last_os_error()).context("mapping shared export");
        }
        unsafe {
            std::ptr::copy_nonoverlapping(samples.as_ptr().cast::<u8>(), address.cast::<u8>(), length);
            libc::munmap(address, length);
        }
        Ok(SharedExport { name, length, offset: 0, width, height, row_bytes,
            format: "rgb32f", byte_order: "native" })
    })();
    unsafe {
        libc::close(fd);
        if result.is_err() { libc::shm_unlink(c_name.as_ptr()); }
    }
    result
}

#[cfg(not(unix))]
pub fn publish(_width: u32, _height: u32, _samples: &[f32]) -> Result<SharedExport> {
    bail!("shared export transport is unavailable on this platform")
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn shares_float_bits_without_clamping_or_quantizing() {
        let samples = [f32::from_bits(0x3f000123), -0.125, 1.25,
            f32::INFINITY, f32::from_bits(0x7fc00123), -0.0];
        let surface = publish(2, 1, &samples).unwrap();
        assert_eq!(surface.row_bytes, 24);
        let name = std::ffi::CString::new(surface.name).unwrap();
        unsafe {
            let fd = libc::shm_open(name.as_ptr(), libc::O_RDONLY, 0);
            assert!(fd >= 0);
            let address = libc::mmap(std::ptr::null_mut(), surface.length,
                libc::PROT_READ, libc::MAP_SHARED, fd, 0);
            assert_ne!(address, libc::MAP_FAILED);
            libc::close(fd);
            assert_eq!(libc::shm_unlink(name.as_ptr()), 0);
            let actual = std::slice::from_raw_parts(address.cast::<u32>(), samples.len());
            assert_eq!(actual, &samples.map(f32::to_bits));
            libc::munmap(address, surface.length);
        }
    }

    #[test]
    fn rejects_invalid_or_oversized_layout_before_allocating() {
        assert!(publish(0, 1, &[]).is_err());
        assert!(publish(2, 1, &[0.0; 3]).is_err());
        assert!(publish(u32::MAX, u32::MAX, &[]).is_err());
    }
}
