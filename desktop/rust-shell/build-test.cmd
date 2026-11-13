@call "C:\Program Files\Microsoft Visual Studio\18\Insiders\VC\Auxiliary\Build\vcvars64.bat" >nul
@cargo +stable-x86_64-pc-windows-msvc build --manifest-path rust-shell\Cargo.toml --release --features test-automation --bin beecode-rust-test
