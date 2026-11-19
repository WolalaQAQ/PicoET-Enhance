// Module-owned control for the existing three-camera adapter and IPC daemon.
#include "camera_service.h"
#include "platform.h"
#include <cerrno>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <sstream>
#include <stdexcept>
#include <vector>
#include <arpa/inet.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

namespace {
const std::string module="/data/adb/modules/picoet-enhance";
const std::string state_root="/data/adb/picoet-enhance";
const std::string runtime_root="/data/local/tmp/picoet-camera";
struct Instance { std::string boot, stage, key; uint64_t pid=0,birth=0; } instance;
std::string read(const std::string& path) {
    std::ifstream file(path); std::ostringstream text; text<<file.rdbuf(); return text.str();
}
void write(const std::string& path,const std::string& value) {
    const std::string temporary=path+".new";
    int fd=open(temporary.c_str(),O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW,0600);
    if(fd<0)throw std::runtime_error("state_write");
    size_t at=0;
    while(at<value.size()) {
        const ssize_t n=::write(fd,value.data()+at,value.size()-at);
        if(n<0 && errno==EINTR)continue;
        if(n<=0){close(fd);throw std::runtime_error("state_write");}
        at+=size_t(n);
    }
    if(close(fd) || rename(temporary.c_str(),path.c_str()))throw std::runtime_error("state_write");
}
void directory(const std::string& path) {
    if(mkdir(path.c_str(),0700) && errno!=EEXIST)throw std::runtime_error("runtime_directory");
    if(ipc::private_directory(path))throw std::runtime_error("runtime_directory");
}
std::string command(const std::vector<std::string>& arguments) {
    int pipes[2]; if(pipe2(pipes,O_CLOEXEC))throw std::runtime_error("command_start");
    pid_t child=fork();
    if(child==0) {
        dup2(pipes[1],STDOUT_FILENO); dup2(pipes[1],STDERR_FILENO);
        close(pipes[0]);close(pipes[1]);
        std::vector<char*> argv; for(const auto& arg:arguments)argv.push_back(const_cast<char*>(arg.c_str()));argv.push_back(nullptr);
        execv(argv[0],argv.data()); _exit(127);
    }
    close(pipes[1]);
    if(child<0){close(pipes[0]);throw std::runtime_error("command_start");}
    std::string output; char buffer[2048];ssize_t n;
    while((n=::read(pipes[0],buffer,sizeof(buffer)))!=0){
        if(n<0){if(errno==EINTR)continue;break;}
        output.append(buffer,size_t(n));
    }
    close(pipes[0]);int status=0;
    while(waitpid(child,&status,0)<0)if(errno!=EINTR)throw std::runtime_error("command_wait");
    if(!WIFEXITED(status)||WEXITSTATUS(status))throw std::runtime_error("command_failed");
    return output;
}
void identify() {
    instance.boot=read("/proc/sys/kernel/random/boot_id");
    while(!instance.boot.empty() && instance.boot.back()=='\n')instance.boot.pop_back();
    if(instance.boot.size()!=36)throw std::runtime_error("service_unavailable");
    std::string value;
    try{value=command({"/system/bin/pidof","pxreyetrackingservice"});}catch(const std::exception&){throw std::runtime_error("service_unavailable");}
    std::istringstream input(value);std::string extra;
    if(!(input>>instance.pid)||input>>extra)throw std::runtime_error("service_unavailable");
    int error=0;instance.birth=ipc::birth(instance.pid,error);
    if(!instance.birth)throw std::runtime_error("service_unavailable");
    instance.key=instance.boot+" "+std::to_string(instance.pid)+" "+std::to_string(instance.birth);
    instance.stage=runtime_root+"/"+instance.boot+"-"+std::to_string(instance.pid)+"-"+std::to_string(instance.birth);
}
bool same_instance() { int error=0;return ipc::birth(instance.pid,error)==instance.birth; }
bool owner(camera_service::Owner& value) {
    return camera_service::parse_owner(read(instance.stage+"/camera-status.json"),value) &&
        value.pid==instance.pid && value.birth==instance.birth && same_instance();
}
struct Channel {
    ipc::Mapping mapping;
    Channel(){if(ipc::map_file(mapping,instance.stage+"/video-channel.bin",sizeof(ipc::Channel),false))return;}
    ~Channel(){ipc::unmap_file(mapping);}
    ipc::Channel* get(){return static_cast<ipc::Channel*>(mapping.address);}
};
bool fresh(const ipc::Channel& channel) {
    int error=0;
    const uint64_t beat=channel.daemon_heartbeat.load(std::memory_order_acquire);
    const uint64_t now=ipc::mono_ns();
    return ipc::valid_header(channel) && ipc::birth(channel.daemon_pid,error)==channel.daemon_birth &&
        camera_service::fresh_heartbeat(beat,now);
}
bool reusable_stopped(const ipc::Channel* c) {
    if(!c)return true;
    int error=0;
    const bool daemon_exited=ipc::birth(c->daemon_pid,error)!=c->daemon_birth;
    return camera_service::stopped(*c,instance.pid,instance.birth) ||
        (daemon_exited && (camera_service::unattached_stopped(*c) || camera_service::unattached_failed(*c)));
}
void wait(const std::function<bool()>& ready,const char* error) {
    const auto deadline=ipc::mono_ns()+10000000000ULL;
    do {if(ready())return;ipc::pause_us(20000);}while(ipc::mono_ns()<deadline);
    throw std::runtime_error(error);
}
void camera_command(const char* verb) {
    camera_service::Owner previous{};
    if(!owner(previous))throw std::runtime_error("owner_changed");
    write(instance.stage+"/camera-control.txt",std::string(previous.owner)+" "+std::to_string(previous.command+1)+" "+verb+"\n");
    camera_service::Owner current{};
    wait([&]{return owner(current) && !std::strcmp(current.owner,previous.owner) && current.command==previous.command+1;},"camera_timeout");
    if(current.result)throw std::runtime_error("camera_failed");
    if(!std::strcmp(verb,"stop") && std::strcmp(current.state,"stopped"))throw std::runtime_error("stop_pending");
    if(!std::strcmp(verb,"start") && std::strcmp(current.state,"armed"))throw std::runtime_error("camera_failed");
}
bool enabled(){return read(state_root+"/camera.enabled")=="1\n";}
bool results_enabled(){return read(state_root+"/camera.results")=="1\n";}
bool eyes_enabled(){return read(state_root+"/camera.eyes")=="1\n";}
std::string address() {
    ifaddrs* entries=nullptr;if(getifaddrs(&entries))return {};
    std::string result;
    for(auto* entry=entries;entry;entry=entry->ifa_next) {
        if(!entry->ifa_addr || entry->ifa_addr->sa_family!=AF_INET || std::strcmp(entry->ifa_name,"wlan0"))continue;
        char text[INET_ADDRSTRLEN]{};
        if(inet_ntop(AF_INET,&reinterpret_cast<sockaddr_in*>(entry->ifa_addr)->sin_addr,text,sizeof(text)))result=text;
    }
    freeifaddrs(entries);return result;
}
std::string status(const std::string& operation_error={}) {
    const bool wanted=enabled();bool online=false,results_online=false,eyes_online=false;
    uint64_t clients[3]{},results_clients=0,eye_clients=0;
    std::string error=operation_error,phase=wanted?"waiting":"off";
    if(error.empty()) {
        const std::string failure=read(state_root+"/camera.failure");
        if(failure.compare(0,instance.key.size()+1,instance.key+"\n")==0)error=failure.substr(instance.key.size()+1);
    }
    camera_service::Owner current{};Channel channel;auto* c=channel.get();
    if(error.empty() && !owner(current) && instance.pid &&
       read("/proc/"+std::to_string(instance.pid)+"/maps").find("libpicoet_camera_jpeg.so")!=std::string::npos)
        error="foreign_adapter";
    if(owner(current) && c && camera_service::attached(*c,instance.pid,instance.birth) && c->state==ipc::Ready && fresh(*c)) {
        online=!std::strcmp(current.state,"armed");
        if(online) {
            results_online=c->results_applied==2;
            results_clients=c->stock_subscription ? 1 : 0;
            eyes_online=c->eye_applied==2;
            eye_clients=c->eye_subscription ? 1 : 0;
            uint64_t total=0;
            for(unsigned i=0;i<3;++i){clients[i]=c->images[i].core.connected;total+=clients[i];}
            phase=total || results_clients || eye_clients?"connected":"idle";
            if(!wanted && error.empty())error="stop_pending";
        }
    }
    if(!online && wanted && error.empty())error="service_unavailable";
    if(!error.empty())phase=error=="tracking_busy"||error=="service_unavailable"?"waiting":"error";
    std::ostringstream out;out<<"version=1\nenabled="<<wanted<<"\nonline="<<online<<"\nstate="<<phase
        <<"\naddress="<<address()<<"\nerror="<<error<<"\n";
    const char* names[]={"mouth","eye_left","eye_right"};
    for(unsigned i=0;i<3;++i)out<<names[i]<<".clients="<<clients[i]<<"\n";
    out<<"results.enabled="<<results_enabled()<<"\nresults.online="<<results_online
        <<"\nresults.clients="<<results_clients<<"\n";
    out<<"eyes.enabled="<<eyes_enabled()<<"\neyes.online="<<eyes_online<<"\neyes.clients="<<eye_clients<<"\n";
    return out.str();
}
void copy_library(const char* name) {
    const std::string target=instance.stage+"/"+name;
    std::ifstream input(module+"/lib/"+name,std::ios::binary);
    if(!input)throw std::runtime_error("camera_not_installed");
    std::ostringstream data;data<<input.rdbuf();write(target,data.str());
    static constexpr char label[]="u:object_r:system_file:s0";
    if(chmod(target.c_str(),0644) || setxattr(target.c_str(),"security.selinux",label,sizeof(label),0))
        throw std::runtime_error("camera_file_label");
}
void start_daemon() {
    const char* results=results_enabled()?"results-on":"results-off";
    const char* eyes=eyes_enabled()?"eyes-on":"eyes-off";
    const pid_t child=fork();if(child<0)throw std::runtime_error("daemon_start");
    if(child==0) {
        setsid();const int log=open((instance.stage+"/daemon.log").c_str(),O_WRONLY|O_CREAT|O_APPEND,0600);
        const int input=open("/dev/null",O_RDONLY);
        if(log<0 || input<0)_exit(126);
        dup2(input,STDIN_FILENO);dup2(log,STDOUT_FILENO);dup2(log,STDERR_FILENO);
        if(input>2)close(input);
        if(log>2)close(log);
        const std::string binary=module+"/bin/ptj_ipc_daemon";
        execl(binary.c_str(),binary.c_str(),instance.stage.c_str(),"45681",results,eyes,nullptr);_exit(127);
    }
    wait([]{Channel channel;auto* c=channel.get();return c && fresh(*c) && c->state==ipc::Ready;},"daemon_start");
}
void sync_results(bool allowed, bool eyes=false) {
    Channel channel;auto* c=channel.get();
    if(!c || c->state!=ipc::Ready || !fresh(*c) || !camera_service::attached(*c,instance.pid,instance.birth))return;
    auto& applied=eyes?c->eye_applied:c->results_applied;
    auto& request=eyes?c->eye_request:c->results_request;
    if(!applied)throw std::runtime_error("results_unavailable");
    const uint32_t policy=allowed?2:1;
    request.store(policy,std::memory_order_release);
    wait([&]{return c->state==ipc::Ready && applied==policy && fresh(*c);},"results_timeout");
}
void retire_dead_instance() {
    const std::string saved=read(state_root+"/camera.instance");
    if(saved.empty() || saved==instance.key)return;
    std::istringstream values(saved);std::string boot;uint64_t pid=0,birth=0;
    if(!(values>>boot>>pid>>birth) || boot.size()!=36)throw std::runtime_error("owner_changed");
    if(boot!=instance.boot)return; // PIDs can repeat across boots; never signal from an old boot record.
    int error=0;
    if(ipc::birth(pid,error)==birth)throw std::runtime_error("owner_changed");
    const std::string previous=runtime_root+"/"+boot+"-"+std::to_string(pid)+"-"+std::to_string(birth);
    ipc::Mapping mapping;
    if(ipc::map_file(mapping,previous+"/video-channel.bin",sizeof(ipc::Channel),false))return;
    auto& channel=*static_cast<ipc::Channel*>(mapping.address);
    try {
        if(ipc::valid_header(channel) && ipc::birth(channel.daemon_pid,error)==channel.daemon_birth) {
            if(kill(pid_t(channel.daemon_pid),SIGTERM) && errno!=ESRCH)throw std::runtime_error("stop_pending");
            wait([&]{return channel.stop_ack && (channel.state==ipc::Stopped || channel.state==ipc::Failed);},"stop_pending");
        }
    }catch(...){ipc::unmap_file(mapping);throw;}
    ipc::unmap_file(mapping);
}
void start() {
    retire_dead_instance();
    camera_service::Owner current{};
    if(owner(current)) {
        Channel channel;auto* c=channel.get();
        if(c && fresh(*c) && c->state==ipc::Ready && camera_service::attached(*c,instance.pid,instance.birth) && !std::strcmp(current.state,"armed"))return;
    }
    if(address().empty())throw std::runtime_error("service_unavailable");
    if(!owner(current)) {
        if(read("/proc/"+std::to_string(instance.pid)+"/maps").find("libpicoet_camera_jpeg.so")!=std::string::npos)
            throw std::runtime_error("foreign_adapter");
        const std::string tracking=command({"/system/bin/dumpsys","-t","1","pxreyetrackingservice"});
        if(!camera_service::idle_tracking(tracking))
            throw std::runtime_error("tracking_busy");
        directory(runtime_root);directory(instance.stage);
        write(state_root+"/camera.instance",instance.key);
        // Reserve the attempt before ptrace; an interrupted attempt is not retried by the watchdog.
        write(state_root+"/camera.failure",instance.key+"\ncamera_failed");
        copy_library("libptj_source.so");copy_library("libpicoet_camera_jpeg.so");
        if(!same_instance())throw std::runtime_error("owner_changed");
        const std::string log=command({module+"/bin/picoet-inject",std::to_string(instance.pid),instance.stage+"/libpicoet_camera_jpeg.so"});
        write(instance.stage+"/inject.log",log);
        wait([&]{return owner(current);},"camera_timeout");
    }
    {
        Channel channel;auto* c=channel.get();
        if(c) {
            if(fresh(*c) && c->state==ipc::Ready && camera_service::attached(*c,instance.pid,instance.birth) && !std::strcmp(current.state,"armed"))return;
            if(!reusable_stopped(c))throw std::runtime_error("stop_pending");
        }
    }
    const std::string file=instance.stage+"/video-channel.bin";
    if(unlink(file.c_str()) && errno!=ENOENT)throw std::runtime_error("channel_remove");
    start_daemon();camera_command("start");
    wait([]{Channel channel;auto* c=channel.get();return c && fresh(*c) && c->state==ipc::Ready && camera_service::attached(*c,instance.pid,instance.birth);},"attach_timeout");
}
void stop() {
    retire_dead_instance();
    camera_service::Owner current{};if(!owner(current))return;
    {
        Channel channel;auto* c=channel.get();
        if(reusable_stopped(c) && (!std::strcmp(current.state,"idle") || !std::strcmp(current.state,"stopped")))return;
    }
    camera_command("stop");
    {
        Channel channel;auto* c=channel.get();
        if(c && ipc::valid_header(*c) && !c->client_ready && fresh(*c)) {
            if(kill(pid_t(c->daemon_pid),SIGTERM) && errno!=ESRCH)throw std::runtime_error("stop_pending");
        }
    }
    wait([]{Channel channel;return reusable_stopped(channel.get());},"stop_pending");
}
}
int main(int argc,char** argv) {
    if(getuid()!=0 || argc!=2)return 2;
    const std::string action=argv[1];
    const bool eyes_action=action=="eyes-on"||action=="eyes-off";
    const bool results_action=action=="results-on"||action=="results-off"||eyes_action;
    if(action!="on"&&action!="off"&&action!="status"&&action!="reconcile"&&action!="halt"&&!results_action)return 2;
    int lock=-1;std::string error;
    try {
        if(action!="status") {
            lock=open((state_root+"/control.lock").c_str(),O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0600);
            if(lock<0 || flock(lock,LOCK_EX))throw std::runtime_error("control_busy");
            if(action=="on" || action=="off")write(state_root+"/camera.enabled",action=="on"?"1\n":"0\n");
        }
        identify();
        if(results_action) {
            const bool allowed=action=="results-on"||action=="eyes-on";
            write(state_root+(eyes_action?"/camera.eyes":"/camera.results"),allowed?"1\n":"0\n");
            sync_results(allowed,eyes_action);
        } else if(action!="status") {
            const std::string failed=read(state_root+"/camera.failure");
            if(action=="reconcile" && failed.compare(0,instance.key.size()+1,instance.key+"\n")==0) {
                // A failed instance requires an explicit user action, not another ptrace attempt.
            } else {
                if(action=="halt" || !enabled())stop();else {start();sync_results(results_enabled());sync_results(eyes_enabled(),true);}
                unlink((state_root+"/camera.failure").c_str());
            }
        }
    } catch(const std::exception& failure) {
        error=failure.what();
        if(error=="service_unavailable" && instance.boot.size()==36 &&
           (action=="off" || action=="halt" || (action=="reconcile" && !enabled()))) {
            try { retire_dead_instance(); error.clear(); } catch(const std::exception& retired) {error=retired.what();}
        }
        if(lock>=0 && !results_action && !error.empty() && error!="control_busy" && error!="tracking_busy" && error!="service_unavailable")
            try{write(state_root+"/camera.failure",instance.key+"\n"+error);}catch(const std::exception&){}
    }
    std::printf("%s",status(error).c_str());
    if(lock>=0)close(lock);
    return error.empty()?0:1;
}
