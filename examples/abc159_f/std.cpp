#include<iostream>
#include<cstdio>
#define ll long long
#define mod 998244353
using namespace std;
ll dp[3005],n,a[3005],s,z[3005],ans;
int main(){
	scanf("%lld%lld",&n,&s);
	for(ll i=1;i<=n;i++){
		scanf("%lld",&a[i]);
		for(ll j=s-a[i];j>=1;j--){
			dp[j+a[i]]+=dp[j];
			dp[j+a[i]]%=mod;
		}
		dp[a[i]]+=i;
		dp[a[i]]%=mod;
		ans+=dp[s];
		ans%=mod;
	}
	printf("%lld",ans);
	return 0;
} 
