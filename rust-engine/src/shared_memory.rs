//! Size output mappings before touching them. Linux tmpfs can accept ftruncate
//! without backing the pages: reserve them so a full /dev/shm returns ENOSPC
//! here instead of raising SIGBUS during the later pixel copy.
use anyhow::{Context, Result};

pub fn size_output(fd: libc::c_int, length: usize) -> Result<()> {
    let length = libc::off_t::try_from(length).context("shared-memory size overflow")?;
    if unsafe { libc::ftruncate(fd, length) } != 0 {
        return Err(std::io::Error::last_os_error()).context("sizing shared memory");
    }
    #[cfg(target_os = "linux")]
    {
        let error = unsafe { libc::posix_fallocate(fd, 0, length) };
        if error != 0 {
            // posix_fallocate returns errno directly; it does not set errno.
            return Err(std::io::Error::from_raw_os_error(error))
                .context("reserving shared memory in /dev/shm");
        }
    }
    Ok(())
}
