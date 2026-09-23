#define _GNU_SOURCE
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>

static int hostpid(void) { char s[64]; ssize_t n=readlink("/proc/self",s,sizeof(s)-1); if(n<0) exit(4); s[n]=0; return atoi(s); }
static volatile sig_atomic_t resumed;
static void resume_handler(int sig) { (void)sig; resumed=1; }
static unsigned char *mem;
static size_t bytes=16*1024*1024;
static const char *root;
static void reply(int seq, const char *type, int snapshot) {
    char path[4096], temp[4096]; uint64_t hash=1469598103934665603ULL;
    for(size_t i=0;i<bytes;i++){hash^=mem[i];hash*=1099511628211ULL;}
    snprintf(path,sizeof(path),"%s/%s-%d.json",root,type,seq);
    snprintf(temp,sizeof(temp),"%s/tmp-%d",root,hostpid());
    FILE *f=fopen(temp,"w"); if(!f) exit(5);
    fprintf(f,"{\"host_pid\":%d,\"namespace_pid\":%d,\"seq\":%d,\"snapshot\":%d,\"hash\":\"%016lx\",\"address\":%lu,\"bytes\":%lu,\"samples\":[%d,%d,%d,%d]}\n",hostpid(),getpid(),seq,snapshot,hash,(unsigned long)mem,(unsigned long)bytes,mem[0],mem[4096],mem[3*4096],mem[17*4096]);
    fclose(f); if(rename(temp,path)) exit(6);
}
int main(int argc,char**argv){
    if(argc!=2)return 2;
    root=argv[1]; signal(SIGUSR1,resume_handler);
    mem=mmap(NULL,bytes,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(mem==MAP_FAILED)return 3;
    memset(mem,0x5a,bytes);
    reply(0,"ready",0); int last=0; char path[4096];snprintf(path,sizeof(path),"%s/command",root);
    for(;;){
        int seq=0,x=0,y=0;char cmd=0;FILE*f=fopen(path,"r");
        if(f){int n=fscanf(f,"%d %c %d %d",&seq,&cmd,&x,&y);fclose(f);if(n!=4)seq=0;}
        if(seq<=last){usleep(1000);continue;} last=seq;
        if(cmd=='w'){if(x<0||(size_t)x>=bytes/4096)return 7;mem[x*4096]=(unsigned char)y;}
        if(cmd=='z'){void *at=mem+(size_t)x*4096; if(x<0||(size_t)x>=bytes/4096)return 7; if(munmap(at,4096))return 8; if(mmap(at,4096,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED,-1,0)!=at)return 9;memset(at,y,4096);}
        if(cmd=='f'){
            pid_t p=syscall(SYS_clone,CLONE_NEWPID|SIGCHLD,NULL,NULL,NULL,0);
            if(p<0){perror("clone");return 10;}
            if(!p){if(setsid()<0)return 12;reply(seq,"snapshot",1);while(!resumed)pause();resumed=0;continue;}
            int status; if(waitpid(p,&status,WUNTRACED)!=p||!WIFSTOPPED(status))return 11;
        }
        reply(seq,"response",0);
    }
}
