#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#ifndef WORKERS
#define WORKERS 4
#endif

#define SIZE (64UL*1024*1024)
#define PAGE 4096UL
#define PAGES (SIZE/PAGE)
static const char *marker(void) { const char *p=getenv("VERIFY_MARKER"); return p?p:"/tmp/official_fork_state.txt"; }
static int port(void) { const char *p=getenv("VERIFY_PORT"); return p?atoi(p):38765; }
static double ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);return 1000.0*t.tv_sec+t.tv_nsec/1e6; }
static void die(const char *s) { perror(s);exit(2); }
static void sendall(int fd,const char *s) { size_t n=strlen(s);while(n) {ssize_t w=send(fd,s,n,0);if(w<=0)die("send");s+=w;n-=w;} }
static void line(int fd,char *out,size_t size) { size_t n=0;while(n+1<size) {ssize_t r=recv(fd,out+n,size-1-n,0);if(r<0&&errno==EINTR)continue;if(r<=0)break;n+=r;if(memchr(out,'\n',n))break;}out[n]=0; }
static uint64_t expected(void) { uint64_t s=0;for(size_t p=0;p<PAGES;p++)s+=p%251;return s; }
static volatile unsigned char *state_buf;
static pthread_barrier_t begin_read, end_read;
static struct {uint64_t sum;unsigned errors;} partial[WORKERS];
static void *read_worker(void *arg) {
 size_t id=(size_t)(uintptr_t)arg;
 for(;;){
  pthread_barrier_wait(&begin_read);
  uint64_t sum=0;unsigned errors=0;
  for(size_t p=PAGES*id/WORKERS;p<PAGES*(id+1)/WORKERS;p++){
   unsigned v=state_buf[p*PAGE];sum+=v;errors+=(v!=p%251);
  }
  partial[id].sum=sum;partial[id].errors=errors;
  pthread_barrier_wait(&end_read);
 }
 return NULL;
}
static void serve(const char *token,int argc,char **argv) {
 volatile unsigned char *buf=mmap(NULL,SIZE,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 if(buf==MAP_FAILED)die("mmap");
 for(size_t p=0;p<PAGES;p++)buf[p*PAGE]=(unsigned char)(p%251);
#ifdef VERIFY_TESTING
 if(argc==4){long p=strtol(argv[3],NULL,10);if(p<0||p>=PAGES)exit(2);buf[p*PAGE]^=1;}
#else
 if(argc!=3)exit(2);
#endif
 state_buf=buf;
 if(pthread_barrier_init(&begin_read,NULL,WORKERS+1)||pthread_barrier_init(&end_read,NULL,WORKERS+1))die("barrier init");
 pthread_t workers[WORKERS];
 for(size_t i=0;i<WORKERS;i++)if(pthread_create(&workers[i],NULL,read_worker,(void*)(uintptr_t)i))die("pthread_create");
 FILE *f=fopen(marker(),"w");if(!f)die("marker");fprintf(f,"official-fork-state token=%s bytes=%lu pid=%ld\n",token,SIZE,(long)getpid());if(fclose(f))die("fclose");
 int fd=socket(AF_INET,SOCK_STREAM,0),yes=1;if(fd<0)die("socket");setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
 struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(port()),.sin_addr.s_addr=htonl(INADDR_LOOPBACK)};
 if(bind(fd,(void*)&a,sizeof(a))||listen(fd,128))die("listen");
 unsigned requests=0;
 for(;;){int c=accept(fd,NULL,NULL);if(c<0){if(errno==EINTR)continue;die("accept");}char in[512],out[1024];line(c,in,sizeof(in));
  double start=ms();uint64_t sum=0;unsigned errors=0;
  // Volatile loads ensure that every one of the 16,384 pages is actually read.
  pthread_barrier_wait(&begin_read);pthread_barrier_wait(&end_read);
  for(size_t i=0;i<WORKERS;i++){sum+=partial[i].sum;errors+=partial[i].errors;}
  double read_ms=ms()-start;requests++;
  snprintf(out,sizeof(out),"OK token=%s bytes=%lu checksum=%"PRIu64" requests=%u pid=%ld pages=%lu errors=%u workers=%u read_ms=%.6f\n",token,SIZE,sum,requests,(long)getpid(),PAGES,errors,WORKERS,read_ms);
  sendall(c,out);close(c);
 }
}
static int verify(const char *token,unsigned want) {
 int fd=socket(AF_INET,SOCK_STREAM,0);if(fd<0)die("socket");
 struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(port()),.sin_addr.s_addr=htonl(INADDR_LOOPBACK)};
 if(connect(fd,(void*)&a,sizeof(a)))die("connect");sendall(fd,"verify\n");char out[1024];line(fd,out,sizeof(out));close(fd);
 char got[256],mt[256],extra;unsigned requests,errors,workers;unsigned long bytes,pages,mb;long pid,mp;uint64_t checksum;double read_ms;
 int fields=sscanf(out,"OK token=%255s bytes=%lu checksum=%"SCNu64" requests=%u pid=%ld pages=%lu errors=%u workers=%u read_ms=%lf %c",got,&bytes,&checksum,&requests,&pid,&pages,&errors,&workers,&read_ms,&extra);
 FILE *f=fopen(marker(),"r");if(!f)die("read marker");int mf=fscanf(f,"official-fork-state token=%255s bytes=%lu pid=%ld",mt,&mb,&mp);fclose(f);
 if(fields!=9||mf!=3||strcmp(token,got)||strcmp(token,mt)||bytes!=SIZE||mb!=SIZE||checksum!=expected()||requests!=want||pages!=PAGES||errors||workers!=WORKERS||pid!=mp){fprintf(stderr,"Verification failed: %s",out);return 3;}
 fputs(out,stdout);return 0;
}
int main(int argc,char **argv) {
 signal(SIGPIPE,SIG_IGN);
 if(argc<3||strlen(argv[2])>200)return 2;
 if(!strcmp(argv[1],"serve"))serve(argv[2],argc,argv);
 if(!strcmp(argv[1],"verify")&&argc==4)return verify(argv[2],(unsigned)strtoul(argv[3],NULL,10));
 return 2;
}
