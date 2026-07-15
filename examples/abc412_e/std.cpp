#include<iostream>
#include<cstdio>
#include<bitset>
#include<cmath>
#define ll long long
#define LL __int128
using namespace std;
const int N=10000002;
ll l,r,ans=1;
int prime[N],tot=0;
bitset<N> f,vis;
int main(){
	scanf("%lld%lld",&l,&r);
	f[1]=0,l++;
	ll st=sqrtl(r);
	for(ll i=2;i<=st;++i){
		if(!f[i])
			prime[++tot]=i;
		for(int j=1;j<=tot&&(ll)prime[j]*i<=st;++j){
			f[(ll)prime[j]*i]=1;
			if(i==(i/prime[j]*prime[j]))
				break;
		}
	}
	for(int i=1;i<=tot;++i){
		LL x=prime[i];
		for(LL now=x;now<=r;now*=x){
			if(now>=l)
				ans++;
		}
		for(LL now=(l+x-1)/x*x;now<=r;now+=x){
			if(now^x)
				vis[now-l]=1;
		}
	}
	for(ll i=l;i<=r;++i){
		if(i>st&&(!vis[i-l]))
			ans++;
	}
	printf("%d\n",ans);
	return 0;
}//