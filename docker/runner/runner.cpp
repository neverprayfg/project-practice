#include <chrono>
#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <regex>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace fs = std::filesystem;

struct ProcessResult {
    int exit_code = 1;
    bool timed_out = false;
    std::string stdout_text;
    std::string stderr_text;
};

struct GenerateRequest {
    std::string subtask;
    std::string seed;
    std::string output_file;
};

struct ValidationRequest {
    std::string input_file;
    std::string output_file;
};

struct ValidationResult {
    ValidationRequest request;
    ProcessResult validation;
    ProcessResult solution;
    bool solution_ran = false;
};

std::string json_escape(const std::string& value) {
    std::string output;
    output.reserve(value.size());
    for (const unsigned char character : value) {
        switch (character) {
            case '\\': output += "\\\\"; break;
            case '"': output += "\\\""; break;
            case '\n': output += "\\n"; break;
            case '\r': output += "\\r"; break;
            case '\t': output += "\\t"; break;
            default:
                if (character >= 0x20) {
                    output += static_cast<char>(character);
                }
        }
    }
    return output;
}

std::string read_limited(const fs::path& path, std::size_t limit = 16000) {
    std::ifstream input(path, std::ios::binary);
    std::string value(limit, '\0');
    input.read(value.data(), static_cast<std::streamsize>(limit));
    value.resize(static_cast<std::size_t>(input.gcount()));
    return value;
}

int configured_timeout() {
    const char* raw = std::getenv("RUNNER_TIMEOUT_SECONDS");
    if (raw == nullptr) return 30;
    try {
        const int value = std::stoi(raw);
        return value >= 1 && value <= 300 ? value : 30;
    } catch (...) {
        return 30;
    }
}

ProcessResult run_process(
    const std::vector<std::string>& arguments,
    const fs::path& stdin_path = "/dev/null",
    const fs::path& stdout_path = ""
) {
    const fs::path capture_out = stdout_path.empty() ? fs::path("/tmp/stdout.log") : stdout_path;
    const fs::path capture_err = "/tmp/stderr.log";
    const pid_t child = fork();
    if (child == -1) {
        return {
            .exit_code = 127,
            .timed_out = false,
            .stdout_text = "",
            .stderr_text = "fork failed",
        };
    }

    if (child == 0) {
        const int input_fd = open(stdin_path.c_str(), O_RDONLY);
        const int output_fd = open(capture_out.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        const int error_fd = open(capture_err.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (input_fd < 0 || output_fd < 0 || error_fd < 0) _exit(126);
        dup2(input_fd, STDIN_FILENO);
        dup2(output_fd, STDOUT_FILENO);
        dup2(error_fd, STDERR_FILENO);
        close(input_fd);
        close(output_fd);
        close(error_fd);

        std::vector<char*> argv;
        argv.reserve(arguments.size() + 1);
        for (const std::string& argument : arguments) {
            argv.push_back(const_cast<char*>(argument.c_str()));
        }
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127);
    }

    int status = 0;
    const auto deadline = std::chrono::steady_clock::now()
        + std::chrono::seconds(configured_timeout());
    bool timed_out = false;
    while (waitpid(child, &status, WNOHANG) == 0) {
        if (std::chrono::steady_clock::now() >= deadline) {
            timed_out = true;
            kill(child, SIGKILL);
            waitpid(child, &status, 0);
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }

    int exit_code = 1;
    if (timed_out) exit_code = 124;
    else if (WIFEXITED(status)) exit_code = WEXITSTATUS(status);
    else if (WIFSIGNALED(status)) exit_code = 128 + WTERMSIG(status);

    return {
        .exit_code = exit_code,
        .timed_out = timed_out,
        .stdout_text = stdout_path.empty() ? read_limited(capture_out) : "",
        .stderr_text = read_limited(capture_err),
    };
}

bool valid_project_id(const std::string& value) {
    static const std::regex pattern("^[0-9a-f]{32}$");
    return std::regex_match(value, pattern);
}

bool valid_relative_path(const std::string& value, const std::string& extension) {
    static const std::regex pattern(
        "^(preview/[0-9a-f]{12}|data/[1-9][0-9]*_[1-9][0-9]*)\\.(in|out)$"
    );
    if (!std::regex_match(value, pattern)) return false;
    return fs::path(value).extension() == extension;
}

void write_process_json(
    std::ostream& output,
    const ProcessResult& result,
    const std::string& output_file = ""
) {
    output << "{\"ok\":" << (result.exit_code == 0 ? "true" : "false")
           << ",\"exit_code\":" << result.exit_code
           << ",\"stdout\":\"" << json_escape(result.stdout_text) << "\""
           << ",\"stderr\":\"" << json_escape(result.stderr_text) << "\"";
    if (!output_file.empty()) {
        output << ",\"output_file\":\"" << json_escape(output_file) << "\"";
    }
    output << "}";
}

void emit(const ProcessResult& result, const std::string& output_file = "") {
    write_process_json(std::cout, result, output_file);
    std::cout << "\n";
}

std::vector<std::string> split_fields(const std::string& value) {
    std::vector<std::string> fields;
    std::size_t start = 0;
    while (true) {
        const std::size_t separator = value.find('|', start);
        fields.push_back(value.substr(start, separator - start));
        if (separator == std::string::npos) break;
        start = separator + 1;
    }
    return fields;
}

void emit_generation_batch(
    const std::vector<GenerateRequest>& requests,
    const std::vector<ProcessResult>& results
) {
    bool ok = true;
    for (const ProcessResult& result : results) ok = ok && result.exit_code == 0;
    std::cout << "{\"ok\":" << (ok ? "true" : "false") << ",\"results\":[";
    for (std::size_t index = 0; index < results.size(); ++index) {
        if (index > 0) std::cout << ',';
        std::cout << "{\"output_file\":\"" << json_escape(requests[index].output_file)
                  << "\",\"result\":";
        write_process_json(std::cout, results[index], requests[index].output_file);
        std::cout << '}';
    }
    std::cout << "]}\n";
}

void emit_validation_batch(const std::vector<ValidationResult>& results) {
    bool ok = true;
    for (const ValidationResult& result : results) {
        ok = ok && result.validation.exit_code == 0
            && result.solution_ran && result.solution.exit_code == 0;
    }
    std::cout << "{\"ok\":" << (ok ? "true" : "false") << ",\"results\":[";
    for (std::size_t index = 0; index < results.size(); ++index) {
        if (index > 0) std::cout << ',';
        const ValidationResult& result = results[index];
        std::cout << "{\"input_file\":\"" << json_escape(result.request.input_file)
                  << "\",\"output_file\":\"" << json_escape(result.request.output_file)
                  << "\",\"validation\":";
        write_process_json(std::cout, result.validation);
        std::cout << ",\"solution\":";
        if (result.solution_ran) {
            write_process_json(std::cout, result.solution, result.request.output_file);
        } else {
            std::cout << "null";
        }
        std::cout << '}';
    }
    std::cout << "]}\n";
}

ProcessResult invalid_request(const std::string& message) {
    return {
        .exit_code = 64,
        .timed_out = false,
        .stdout_text = "",
        .stderr_text = message,
    };
}

int main(int argc, char** argv) {
    if (argc < 3) {
        emit(invalid_request("operation and project id are required"));
        return 0;
    }
    const std::string operation = argv[1];
    const std::string project_id = argv[2];
    if (!valid_project_id(project_id)) {
        emit(invalid_request("invalid project id"));
        return 0;
    }

    const fs::path project = fs::path("/workspace") / project_id;
    if (!fs::is_directory(project)) {
        emit(invalid_request("project directory does not exist"));
        return 0;
    }

    if (operation == "compile" && argc == 4) {
        const std::string role = argv[3];
        fs::path source;
        if (role == "solution") source = project / "source/solution.cpp";
        else if (role == "generator") source = project / "generated/generator.cpp";
        else if (role == "validator") source = project / "generated/validator.cpp";
        else {
            emit(invalid_request("invalid compile role"));
            return 0;
        }
        const fs::path binary = project / "bin" / role;
        fs::create_directories(binary.parent_path());
        emit(run_process({
            "g++", "-std=c++17", "-O2", "-pipe", "-Wall", "-Wextra",
            "-I/opt/testlib", "-I/opt/jngen", source.string(), "-o", binary.string()
        }));
        return 0;
    }

    if (operation == "generate" && argc == 6) {
        const std::string subtask = argv[3];
        const std::string seed = argv[4];
        const std::string relative = argv[5];
        if (!std::regex_match(subtask, std::regex("^[1-9][0-9]*$"))
            || !std::regex_match(seed, std::regex("^-?[0-9]+$"))
            || !valid_relative_path(relative, ".in")) {
            emit(invalid_request("invalid generation arguments"));
            return 0;
        }
        const fs::path output = project / relative;
        fs::create_directories(output.parent_path());
        emit(
            run_process({
                (project / "bin/generator").string(),
                "-subtask=" + subtask,
                "-seed=" + seed
            }, "/dev/null", output),
            relative
        );
        return 0;
    }

    if (operation == "generate-batch" && argc >= 4 && argc <= 67) {
        std::vector<GenerateRequest> requests;
        requests.reserve(static_cast<std::size_t>(argc - 3));
        for (int index = 3; index < argc; ++index) {
            const std::vector<std::string> fields = split_fields(argv[index]);
            if (fields.size() != 3
                || !std::regex_match(fields[0], std::regex("^[1-9][0-9]*$"))
                || !std::regex_match(fields[1], std::regex("^-?[0-9]+$"))
                || !valid_relative_path(fields[2], ".in")) {
                emit(invalid_request("invalid generation batch arguments"));
                return 0;
            }
            requests.push_back({fields[0], fields[1], fields[2]});
        }

        std::vector<ProcessResult> results;
        results.reserve(requests.size());
        for (const GenerateRequest& request : requests) {
            const fs::path output = project / request.output_file;
            fs::create_directories(output.parent_path());
            results.push_back(run_process({
                (project / "bin/generator").string(),
                "-subtask=" + request.subtask,
                "-seed=" + request.seed
            }, "/dev/null", output));
        }
        emit_generation_batch(requests, results);
        return 0;
    }

    if (operation == "validate" && argc == 4) {
        const std::string relative = argv[3];
        if (!valid_relative_path(relative, ".in")) {
            emit(invalid_request("invalid validation path"));
            return 0;
        }
        emit(run_process({(project / "bin/validator").string()}, project / relative));
        return 0;
    }

    if (operation == "solve" && argc == 5) {
        const std::string input_relative = argv[3];
        const std::string output_relative = argv[4];
        if (!valid_relative_path(input_relative, ".in")
            || !valid_relative_path(output_relative, ".out")
            || fs::path(input_relative).stem() != fs::path(output_relative).stem()) {
            emit(invalid_request("invalid solution paths"));
            return 0;
        }
        const fs::path output = project / output_relative;
        fs::create_directories(output.parent_path());
        emit(
            run_process({(project / "bin/solution").string()}, project / input_relative, output),
            output_relative
        );
        return 0;
    }

    if (operation == "validate-solve-batch" && argc >= 4 && argc <= 67) {
        std::vector<ValidationRequest> requests;
        requests.reserve(static_cast<std::size_t>(argc - 3));
        for (int index = 3; index < argc; ++index) {
            const std::vector<std::string> fields = split_fields(argv[index]);
            if (fields.size() != 2
                || !valid_relative_path(fields[0], ".in")
                || !valid_relative_path(fields[1], ".out")
                || fs::path(fields[0]).stem() != fs::path(fields[1]).stem()) {
                emit(invalid_request("invalid validation batch arguments"));
                return 0;
            }
            requests.push_back({fields[0], fields[1]});
        }

        std::vector<ValidationResult> results;
        results.reserve(requests.size());
        for (const ValidationRequest& request : requests) {
            ValidationResult result;
            result.request = request;
            result.validation = run_process(
                {(project / "bin/validator").string()},
                project / request.input_file
            );
            if (result.validation.exit_code == 0) {
                const fs::path output = project / request.output_file;
                fs::create_directories(output.parent_path());
                result.solution = run_process(
                    {(project / "bin/solution").string()},
                    project / request.input_file,
                    output
                );
                result.solution_ran = true;
            }
            results.push_back(result);
        }
        emit_validation_batch(results);
        return 0;
    }

    emit(invalid_request("unsupported operation"));
    return 0;
}
