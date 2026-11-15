#pragma once
#include "postprocessing.h"

namespace expression_independent {
constexpr const char kPostProcessingFile[] = "/data/local/tmp/picoet-postprocessing.conf";
constexpr size_t kPostProcessingMaxBytes = 16384;
// Missing file means existing defaults. Other read/parse failures are reported.
bool read_postprocessing_file(PostProcessingConfig& config, std::string& error);
bool write_postprocessing_file(const PostProcessingConfig& config, std::string& error);
}
