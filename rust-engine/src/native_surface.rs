//! Immutable native preview transport. The server adopts each successful name
//! and unlinks it on LRU eviction; mmap users retain their pixels after unlink.
use std::sync::atomic::{AtomicU64, Ordering};
use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct SharedSurface {
    pub name: String,
    pub length: usize,
    pub offset: usize,
    pub width: u32,
    pub height: u32,
    #[serde(rename = "rowBytes")]
    pub row_bytes: usize,
}

pub fn publish_rgb(width: u32, height: u32, samples: &[f32]) -> Result<SharedSurface> {
    let row_bytes = (width as usize).checked_mul(4).context("surface row overflow")?
        .checked_add(255).context("surface alignment overflow")? & !255;
    let pixels = (width as usize).checked_mul(height as usize).context("surface size overflow")?;
    if width == 0 || height == 0 || samples.len() != pixels * 3 {
        bail!("native surface sample count mismatch");
    }
    let mut packed = vec![0_u8; row_bytes.checked_mul(height as usize).context("surface size overflow")?];
    packed.par_chunks_mut(row_bytes).zip(samples.par_chunks(width as usize * 3))
        .for_each(|(row, source)| {
            for (pixel, rgb) in row.chunks_mut(4).zip(source.chunks(3)) {
                for channel in 0..3 {
                    pixel[channel] = (rgb[channel].clamp(0.0, 1.0) * 255.0).round_ties_even() as u8;
                }
                pixel[3] = 255;
            }
        });
    publish_packed(width, height, row_bytes, &packed)
}

#[cfg(unix)]
pub fn publish_packed(width: u32, height: u32, row_bytes: usize, packed: &[u8]) -> Result<SharedSurface> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    if width == 0 || height == 0 || row_bytes < width as usize * 4 || row_bytes % 256 != 0
        || packed.len() != row_bytes.checked_mul(height as usize).context("surface size overflow")? {
        bail!("invalid packed surface");
    }
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) } as usize;
    let length = packed.len().checked_add(page - 1).context("surface mapping overflow")? / page * page;
    if length > 256 * 1024 * 1024 { bail!("shared native surface exceeds byte budget"); }
    let name = format!("/lt-{:x}-{:x}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed));
    let c_name = std::ffi::CString::new(name.clone())?;
    let fd = unsafe { libc::shm_open(c_name.as_ptr(), libc::O_RDWR | libc::O_CREAT | libc::O_EXCL, 0o600) };
    if fd < 0 { return Err(std::io::Error::last_os_error()).context("creating native shared memory"); }
    let result = (|| {
        if unsafe { libc::ftruncate(fd, length as libc::off_t) } != 0 {
            return Err(std::io::Error::last_os_error()).context("sizing native shared memory");
        }
        let address = unsafe { libc::mmap(std::ptr::null_mut(), length, libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_SHARED, fd, 0) };
        if address == libc::MAP_FAILED {
            return Err(std::io::Error::last_os_error()).context("mapping native shared memory");
        }
        unsafe {
            std::ptr::copy_nonoverlapping(packed.as_ptr(), address.cast::<u8>(), packed.len());
            libc::munmap(address, length);
        }
        Ok(SharedSurface { name, length, offset: 0, width, height, row_bytes })
    })();
    unsafe {
        libc::close(fd);
        if result.is_err() { libc::shm_unlink(c_name.as_ptr()); }
    }
    result
}

#[cfg(not(unix))]
pub fn publish_packed(_width: u32, _height: u32, _row_bytes: usize, _packed: &[u8]) -> Result<SharedSurface> {
    bail!("shared native transport is unavailable on this platform")
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn mapping_preserves_padded_rows_and_survives_unlink() {
        let surface = publish_rgb(2, 2, &[0.0, 0.5, 1.0, 1.0, 0.0, 0.25,
            0.25, 0.5, 0.75, 0.0, 0.0, 0.0]).unwrap();
        let name = std::ffi::CString::new(surface.name).unwrap();
        unsafe {
            let fd = libc::shm_open(name.as_ptr(), libc::O_RDONLY, 0);
            assert!(fd >= 0);
            let address = libc::mmap(std::ptr::null_mut(), surface.length, libc::PROT_READ,
                libc::MAP_SHARED, fd, 0);
            assert_ne!(address, libc::MAP_FAILED);
            libc::close(fd);
            assert_eq!(libc::shm_unlink(name.as_ptr()), 0);
            let bytes = std::slice::from_raw_parts(address.cast::<u8>(), surface.length);
            assert_eq!(&bytes[..8], &[0, 128, 255, 255, 255, 0, 64, 255]);
            assert_eq!(&bytes[256..264], &[64, 128, 191, 255, 0, 0, 0, 255]);
            libc::munmap(address, surface.length);
        }
    }
}
