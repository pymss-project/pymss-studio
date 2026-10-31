# spectral

Parametric EQ and filter family, implemented as cascaded scipy biquads
(Audio EQ Cookbook coefficients).

| Capability | Description |
|---|---|
| `highpass` / `lowpass` / `bandpass` / `notch` | Single biquad filters |
| `peak_eq` | Peaking EQ boost/cut |
| `lowshelf` / `highshelf` | Shelving filters |
| `parametric_eq` | Arbitrary cascade of bands |
| `baxandall_eq` | Classic hi-fi bass/treble tone stack |

Install: `pymss install spectral`.
