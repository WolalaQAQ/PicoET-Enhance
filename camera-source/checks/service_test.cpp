#include "camera_service.h"
#include <cstdlib>
#include <memory>
#include <iostream>
unsigned checks=0;
void check(bool value){++checks;if(!value)std::exit(1);}
int main() {
    const std::string idle="ET algorithm is not started.\nFT algorithm is not started.\nIPD algorithm is not started.\nThere is 0 clients\n";
    check(camera_service::idle_tracking(idle));
    check(!camera_service::idle_tracking(idle+"There is 1 clients\n"));
    check(!camera_service::idle_tracking("ET algorithm is started\nFT algorithm is not started\n"));
    check(!camera_service::idle_tracking("ET algorithm is not started\nFT algorithm is started\n"));
    check(!camera_service::idle_tracking(""));
    camera_service::Owner owner{};
    const std::string prefix="{\"schema_version\":1,\"version\":\"camera5-test\",\"pid\":123,\"start_ticks\":456,\"owner_id\":\"0123456789abcdef\",\"monotonic_ns\":900,\"state\":\"armed\",\"command_id\":7,\"command_result\":0}";
    check(camera_service::parse_owner(prefix,owner));check(owner.pid==123&&owner.birth==456&&owner.command==7);
    check(!camera_service::parse_owner("{}",owner));
    auto memory=std::make_unique<ipc::Channel>();auto& c=*memory;
    std::memcpy(c.magic,"PTJIPC5",8);c.session=100;c.daemon_pid=50;c.daemon_birth=60;c.daemon_nonce[0]=10;
    c.client_pid=123;c.client_birth=456;c.request_session=100;c.client_ready=1;c.client_nonce[0]=20;
    c.client_root_echo[0]=10;c.attach_ack=1;c.attach_ack_session=100;c.attach_client_nonce[0]=20;c.attach_root_nonce[0]=10;
    check(camera_service::attached(c,123,456));check(!camera_service::attached(c,123,457));
    c.attach_root_nonce[0]=11;check(!camera_service::attached(c,123,456));c.attach_root_nonce[0]=10;
    c.state=ipc::Stopped;c.stop_ack=1;c.stop_ack_session=100;c.stop_client_nonce[0]=20;
    check(camera_service::stopped(c,123,456));
    for(auto& image:c.images){image.in_use=1;check(!camera_service::stopped(c,123,456));image.in_use=0;
        image.subscription=1;check(!camera_service::stopped(c,123,456));image.subscription=0;
        image.slots[1].state=ipc::Processing;check(!camera_service::stopped(c,123,456));image.slots[1].state=ipc::Free;}
    c.stop_ack_session=101;check(!camera_service::stopped(c,123,456));c.stop_ack_session=100;
    c.stop_client_nonce[0]=21;check(!camera_service::stopped(c,123,456));c.stop_client_nonce[0]=20;
    c.client_ready=0;c.client_pid=0;check(camera_service::unattached_stopped(c));
    c.client_pid=123;check(!camera_service::unattached_stopped(c));
    c.state=ipc::Failed;c.stop_ack=0;c.client_ready=0;c.client_pid=0;check(camera_service::unattached_failed(c));
    c.client_pid=123;check(!camera_service::unattached_failed(c));
    c.client_pid=0;c.state=ipc::Ready;check(!camera_service::unattached_failed(c));
    check(!camera_service::fresh_heartbeat(0,1000));
    check(camera_service::fresh_heartbeat(1000,1000));
    check(camera_service::fresh_heartbeat(1000,1000+ipc::HEARTBEAT_NS-1));
    check(!camera_service::fresh_heartbeat(1000,1000+ipc::HEARTBEAT_NS));
    check(camera_service::fresh_heartbeat(1005,1000));
    check(!camera_service::fresh_heartbeat(1000+ipc::HEARTBEAT_NS,1000));
    std::cout<<checks<<" camera service ownership and stop checks passed\n";
}
